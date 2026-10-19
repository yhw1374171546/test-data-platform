"""定时任务：把"需要人手动跑"的事收进一个常驻进程。

三件事，都是**不跑就会慢慢烂掉**的那种：

    idem_cleanup      幂等键只增不删，久了表会涨到影响写入（索引 002 早就备好了，缺人用）
    recon_daily       日终对账要每天跑，靠人记得 = 迟早忘；忘了就是"差异发现得晚"
    dead_letter_check 死信不会自愈，但只有人看 /metrics 才会发现 —— 变成日志里的 ERROR 才可靠

## 多副本怎么保证只跑一次

用 **PostgreSQL 会话级 advisory lock**（`pg_try_advisory_lock`），不是自己造锁表：

· 锁跟着**会话**走：进程崩了、连接断了，锁自动释放 —— 不会留下"死锁行"要人清；
· `try` 版本拿不到锁立刻返回 false，不会把第二个副本卡住；
· 同一台机器上起 5 个副本，只有一个真的执行，其余的跳过并打一条 info 日志。

为什么不用 Redis 分布式锁：这里已经有 PG，且 advisory lock 没有 TTL/续期的坑；
多引入一个组件只为一把锁，是给自己加故障面。（Redis 锁更适合跨进程协调非 DB 任务。）

## 为什么不用 APScheduler / Celery beat

三个固定节奏的任务，用标准库 + advisory lock 就够。少一个依赖 = 少一个升级/兼容问题，
也少一个面试官问"为什么用它"的坑。真要复杂调度再换。

用法：
    python scripts/scheduler.py                 # 常驻（默认每 60s 检查一次）
    python scripts/scheduler.py --once          # 立刻把所有任务各跑一次（CI / 演示）
    python scripts/scheduler.py --once --job recon_daily
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import text                                  # noqa: E402

from app.domain.biz_time import TZ                            # noqa: E402
from app.infra import metrics                                 # noqa: E402
from app.infra.db import engine, session_scope                # noqa: E402
from app.service.recon import run_recon                       # noqa: E402

logger = logging.getLogger("tdp.scheduler")

DEFAULT_INTERVAL = 60
DEFAULT_IDEM_TTL_DAYS = 7
DEFAULT_IDEM_EVERY_SEC = 6 * 3600
DEFAULT_RECON_AT = "00:10"
DEFAULT_DEAD_EVERY_SEC = 300


# ────────────────────────── 分布式互斥 ──────────────────────────
def _lock_key(name: str) -> int:
    """把任务名压成一个 int4。crc32 足够（冲突只会让两个任务互斥，不会出错）。"""
    return zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF


@contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    """拿到锁 yield True，拿不到 yield False（调用方自己决定跳过）。

    注意：加锁和解锁必须**同一个连接**（会话级锁），所以这里不能借用 session_scope。
    """
    with engine.connect() as conn:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                     {"k": _lock_key(name)}).scalar())
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(name)})


# ────────────────────────── 任务 ──────────────────────────
def cleanup_idempotency(ttl_days: int = DEFAULT_IDEM_TTL_DAYS) -> int:
    """删掉过期的幂等键，返回删除条数。

    为什么按 `created_at` 而不是"最后访问时间"：幂等键的生命周期是
    "调用方可能重试的时间窗"，由创建时间决定；访问时间只会让老键无限续命。
    """
    cutoff = datetime.now(TZ) - timedelta(days=max(1, int(ttl_days)))
    with session_scope() as s:
        result = s.execute(text("DELETE FROM idempotency_keys WHERE created_at < :cut"),
                           {"cut": cutoff})
        deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("清理幂等键 %s 条（早于 %s，ttl=%s 天）", deleted, cutoff.date(), ttl_days)
    else:
        logger.debug("没有需要清理的幂等键（ttl=%s 天）", ttl_days)
    return deleted


def check_dead_letters() -> int:
    """查死信数；>0 打 ERROR（让告警系统/日志关键字接得住），并刷新 gauge。"""
    with session_scope() as s:
        row = s.execute(text("""
            SELECT count(*) FILTER (WHERE status = 'dead')    AS dead,
                   count(*) FILTER (WHERE status = 'pending') AS pending
              FROM outbox
        """)).one()
    dead, pending = int(row.dead), int(row.pending)
    metrics.OUTBOX_DEAD.set(dead)
    metrics.OUTBOX_PENDING.set(pending)
    if dead:
        logger.error("有 %s 条死信待处理（不会自愈）：POST /v1/admin/outbox/replay", dead)
    else:
        logger.debug("死信为 0，pending=%s", pending)
    return dead


def recon_daily(day: Optional[date] = None) -> Dict[str, Any]:
    """对昨天（或指定某天）跑一次对账。差异会落 recon_discrepancies 表 + 指标。

    返回的字段是 `total`（差异条数）/ `by_kind` / `pairs_checked`。
    注意别写成 `diff_count` —— recon 没这个键，取不到会静默变 0，日志就成了假绿灯。
    """
    target = day or (datetime.now(TZ).date() - timedelta(days=1))
    result = run_recon(biz_date=target.isoformat())
    diffs = int(result.get("total", 0) or 0)
    log = logger.error if diffs else logger.info
    log("对账完成 %s：核对 %s 组，差异 %s 条 %s",
        target, result.get("pairs_checked"), diffs, result.get("by_kind") or {})
    return result


JOBS: Dict[str, Callable[..., Any]] = {
    "idem_cleanup": cleanup_idempotency,
    "recon_daily": recon_daily,
    "dead_letter_check": check_dead_letters,
}


# ────────────────────────── 调度 ──────────────────────────
class IntervalSchedule:
    """每 N 秒一次。第一次立即执行（否则启动后要空等一个周期）。"""

    def __init__(self, seconds: float):
        self.seconds = max(1.0, float(seconds))
        self._next = 0.0                       # 0 = 立刻到期

    def is_due(self, now: float) -> bool:
        return now >= self._next

    def advance(self, now: float) -> None:
        self._next = now + self.seconds


class DailySchedule:
    """每天 HH:MM 执行一次（本地时区）。按日期判断，不用 Cron 表达式 —— 只有定点需求。"""

    def __init__(self, at: str):
        hour, minute = (int(x) for x in str(at).split(":"))
        self.hour, self.minute = hour, minute
        self._last_date: Optional[date] = None

    def is_due(self, now: datetime) -> bool:
        if self._last_date == now.date():
            return False
        target = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        return now >= target

    def advance(self, now: datetime) -> None:
        self._last_date = now.date()


def run_job(name: str, **kwargs: Any) -> Any:
    """带锁执行一个任务。拿不到锁 = 别的副本在跑，直接跳过。"""
    with advisory_lock(f"tdp:scheduler:{name}") as acquired:
        if not acquired:
            logger.info("跳过 %s：另一个副本持有锁", name)
            return None
        try:
            return JOBS[name](**kwargs)
        except Exception:                             # noqa: BLE001 - 单个任务失败不能拖垮调度器
            logger.exception("任务 %s 失败", name)
            return None


def run_all(ttl_days: int = DEFAULT_IDEM_TTL_DAYS) -> Dict[str, Any]:
    """把三个任务各跑一次（CI / 演示用）。"""
    return {
        "idem_cleanup": run_job("idem_cleanup", ttl_days=ttl_days),
        "recon_daily": run_job("recon_daily"),
        "dead_letter_check": run_job("dead_letter_check"),
    }


def serve(*, interval: float, idem_ttl_days: int, idem_every: float,
          recon_at: str, dead_every: float) -> int:
    schedules: List[tuple] = [
        ("idem_cleanup", IntervalSchedule(idem_every), {"ttl_days": idem_ttl_days}),
        ("recon_daily", DailySchedule(recon_at), {}),
        ("dead_letter_check", IntervalSchedule(dead_every), {}),
    ]
    logger.info("调度器启动：interval=%ss idem_every=%ss recon_at=%s dead_every=%ss",
                interval, idem_every, recon_at, dead_every)

    stopping = {"flag": False}

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，等当前任务结束后退出", signum)
        stopping["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):                  # pragma: no cover - 非主线程/平台差异
            pass

    while not stopping["flag"]:
        now_mono = time.monotonic()
        now_dt = datetime.now(TZ)
        for name, schedule, kwargs in schedules:
            due = (schedule.is_due(now_mono) if isinstance(schedule, IntervalSchedule)
                   else schedule.is_due(now_dt))
            if not due:
                continue
            run_job(name, **kwargs)
            if isinstance(schedule, IntervalSchedule):
                schedule.advance(time.monotonic())
            else:
                schedule.advance(datetime.now(TZ))
        # 用 Event 更优雅，但这里 60s 的粒度下 sleep 足够，少一个对象
        for _ in range(int(max(1.0, interval))):
            if stopping["flag"]:
                break
            time.sleep(1)
    logger.info("调度器已退出")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="定时任务：幂等键清理 / 每日对账 / 死信巡检")
    ap.add_argument("--once", action="store_true", help="立刻执行一轮然后退出（CI/演示）")
    ap.add_argument("--job", default=None, choices=sorted(JOBS), help="只跑某一个任务")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="轮询间隔（秒）")
    ap.add_argument("--idem-ttl-days", type=int, default=DEFAULT_IDEM_TTL_DAYS)
    ap.add_argument("--idem-every", type=float, default=DEFAULT_IDEM_EVERY_SEC)
    ap.add_argument("--recon-at", default=DEFAULT_RECON_AT, help="每天几点对账 HH:MM")
    ap.add_argument("--dead-every", type=float, default=DEFAULT_DEAD_EVERY_SEC)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("TDP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.job:
        value = run_job(args.job, **({"ttl_days": args.idem_ttl_days}
                                     if args.job == "idem_cleanup" else {}))
        print(f"{args.job} -> {value}")
        return 0

    if args.once:
        results = run_all(ttl_days=args.idem_ttl_days)
        for name, value in results.items():
            summary = value if not isinstance(value, dict) else {
                k: value.get(k) for k in ("pairs_checked", "pairs_with_diff", "total")}
            print(f"{name:<18} -> {summary}")
        return 0

    return serve(interval=args.interval, idem_ttl_days=args.idem_ttl_days,
                 idem_every=args.idem_every, recon_at=args.recon_at,
                 dead_every=args.dead_every)


if __name__ == "__main__":
    sys.exit(main())
