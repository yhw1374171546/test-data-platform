"""本地消息表 worker：**轮询 outbox → 重算日报汇总 → 置 done**（技术方案 §4.4）。

三条设计决策，都是会被追问的点：

**1. 汇总用「重算」而不是「累加」**
   outbox 是 at-least-once：进程可能在"写汇总"和"置 done"之间崩掉，消息会被再处理一次。
   如果每次 `total = total + 1`，重放就多算一条。
   这里改成"按 (日, 项目, 版本) 从 executions 重新聚合，再整体 upsert"：
     · 处理 1 次和处理 100 次的结果完全一样 → 天然幂等；
     · 某条消息丢了，只要同键还有一条消息被处理，整天的汇总就会被重算正确 → **自愈**。
   代价是每条消息要做一次按日聚合。数据量大了之后应该批量化（同一键只算一次），
   或者退回"累加 + 定期全量对账"——但那是规模问题，不是正确性问题。

**2. 一条消息一个事务，用 `FOR UPDATE SKIP LOCKED` 抢**
   多副本部署时每个 worker 都轮询，`SKIP LOCKED` 让它们各拿各的、不互相等。
   事务边界卡在"单条消息"：一条坏消息不会连累同批其它消息，
   也不会让一个长事务把 outbox 表锁住。

**3. 失败 ≠ 丢弃**
   `attempts+1`，`next_at = now() + base * 2^attempts` 指数退避；
   超过 `max_attempts` 置 `dead`（要告警 + 人工重放）。
   未知 topic / payload 缺字段这类"永远不可能成功"的消息**直接 dead**，不浪费 5 次重试。

关于 dead 的真实影响（实验里量过，值得写下来）：
因为汇总是"重算"的，同一 (日,项目,版本) 只要**有一条**消息成功处理，整天就是对的。
所以 dead 造成少算的前提是"这个键一条消息都没成功过"。
但这不代表 dead 可以不管——它意味着 worker 曾经持续失败，
而且重放前你无法确认到底影响哪些键。
"""
from __future__ import annotations

import logging
import os
from datetime import date as date_cls
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text

from app.domain.biz_time import biz_date_sql, biz_day_bounds
from app.infra import cache, metrics
from app.infra.db import SessionLocal, session_scope

logger = logging.getLogger("tdp.outbox")

TOPIC_EXEC_RESULT = "exec_result_recorded"

# 技术方案原文"> 5 次 → dead"，即重试 5 次仍失败就放弃
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("TDP_OUTBOX_MAX_ATTEMPTS", "5"))
DEFAULT_BACKOFF_BASE = float(os.environ.get("TDP_OUTBOX_BACKOFF_BASE", "5"))
DEFAULT_BATCH = int(os.environ.get("TDP_OUTBOX_BATCH", "20"))


class OutboxMessageInvalid(Exception):
    """永远不会成功的消息（未知 topic、payload 缺字段）：直接 dead，不重试。"""


# ────────────────────────── 单条消息的处理 ──────────────────────────
def _apply(session: Any, row: Any) -> None:
    """按 topic 分发。

    本地消息表的价值就在这里：**一张表多个消费者**。
    同一个 worker 既能汇总执行结果（`exec_result_recorded`），
    也能做缓存失效（`cache_invalidate`，技术方案 §4.3 的 C 方案）。
    加新 topic 只需要在这里加一个分支 + 一个处理函数。
    """
    if row.topic == TOPIC_EXEC_RESULT:
        _apply_exec_result(session, row)
        return
    if row.topic == cache.TOPIC_CACHE_INVALIDATE:
        _apply_cache_invalidate(session, row)
        return
    raise OutboxMessageInvalid(f"未知 topic：{row.topic!r}")


def _apply_cache_invalidate(session: Any, row: Any) -> None:
    """C 方案：消费"失效缓存"消息 —— 真正把 key 删掉。

    删失败（Redis 不可用）**必须抛异常**让它重试，不能静默 done：
    失效消息一旦被标记成功而实际没删，缓存就会一直脏到 TTL 过期，
    而这正是"消息驱动删缓存"要避免的事。
    """
    payload = dict(row.payload or {})
    keys = [str(k) for k in (payload.get("keys") or []) if k]
    if not keys:
        raise OutboxMessageInvalid("cache_invalidate 消息里没有 keys")
    if not cache.delete(*keys):
        raise RuntimeError(f"缓存不可用，{len(keys)} 个 key 未失效")
    metrics.CACHE_INVALIDATIONS.labels(reason=str(payload.get("reason") or "outbox")).inc()


def _apply_exec_result(session: Any, row: Any) -> None:
    """重算并 upsert 这一条消息对应的 (日, 项目, 版本) 汇总。"""
    payload = dict(row.payload or {})
    try:
        biz_date = date_cls.fromisoformat(str(payload["biz_date"]))
        project_id = int(payload["project_id"])
        version_id = int(payload["version_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OutboxMessageInvalid(f"payload 缺字段或格式错：{exc}") from exc

    # 项目/版本已经被删（数据保留期到了、或者测试清理）→ 这条消息永远不可能成功。
    # 直接判作废（dead）而不是重试 5 次：重试只会把日志刷满，还会让告警失去意义。
    exists = session.execute(text("""
        SELECT 1 FROM projects p, versions v WHERE p.id = :pid AND v.id = :vid
    """), {"pid": project_id, "vid": version_id}).first()
    if exists is None:
        raise OutboxMessageInvalid(
            f"project={project_id} / version={version_id} 已不存在，消息作废")

    # 用 [本地日 00:00, 次日 00:00) 的 UTC 区间去筛 executed_at，
    # 这样能命中 ix_exec_summary (executed_at, result)；
    # 如果写成 (executed_at AT TIME ZONE tz)::date = :d 就只能全表扫。
    start, end = biz_day_bounds(biz_date)

    counts = session.execute(text("""
        SELECT count(*)                                        AS total,
               count(*) FILTER (WHERE e.result = 'pass')       AS passed,
               count(*) FILTER (WHERE e.result = 'fail')       AS failed,
               count(*) FILTER (WHERE e.result = 'blocked')    AS blocked
          FROM executions e
          JOIN execution_queue q ON q.id = e.queue_id
         WHERE q.project_id = :pid
           AND q.version_id = :vid
           AND e.executed_at >= :start
           AND e.executed_at <  :end
    """), {"pid": project_id, "vid": version_id,
           "start": start, "end": end}).one()

    session.execute(text("""
        INSERT INTO daily_exec_summary
               (biz_date, project_id, version_id, total, passed, failed, blocked)
        VALUES (:biz, :pid, :vid, :total, :passed, :failed, :blocked)
        ON CONFLICT (biz_date, project_id, version_id) DO UPDATE
           SET total   = EXCLUDED.total,
               passed  = EXCLUDED.passed,
               failed  = EXCLUDED.failed,
               blocked = EXCLUDED.blocked
    """), {"biz": biz_date, "pid": project_id, "vid": version_id,
           "total": int(counts.total), "passed": int(counts.passed),
           "failed": int(counts.failed), "blocked": int(counts.blocked)})

    metrics.SUMMARY_UPSERTS.inc()


def _record_failure(msg_id: int, attempts: int, exc: Exception, *,
                    max_attempts: int, backoff_base: float) -> str:
    """把失败写回 outbox（**独立事务**）。

    为什么要独立事务：处理消息的事务可能已经被 DB 错误中止，回滚后连"记录失败"都写不进去。
    代价是回滚到写失败之间有几毫秒窗口，另一个 worker 可能把这条消息再捡起来——
    但 `_apply` 是幂等重算，多处理一次不会算错；而 `WHERE status='pending'` 保证不会误改已 dead 的行。
    """
    new_attempts = attempts + 1
    invalid = isinstance(exc, OutboxMessageInvalid)
    dead = invalid or new_attempts >= max_attempts
    # 指数退避：base, 2*base, 4*base ... ；dead 的消息不需要再排期
    delay = 0.0 if dead else backoff_base * (2 ** attempts)

    with session_scope() as s:
        s.execute(text("""
            UPDATE outbox
               SET attempts   = :attempts,
                   status     = :status,
                   next_at    = now() + CAST(:delay AS DOUBLE PRECISION) * INTERVAL '1 second',
                   last_error = :err
             WHERE id = :id AND status = 'pending'
        """), {"attempts": new_attempts,
               "status": "dead" if dead else "pending",
               "delay": delay,
               "err": f"{type(exc).__name__}: {exc}"[:500],
               "id": msg_id})

    if dead:
        logger.error("outbox 消息进入死信 id=%s attempts=%s err=%s", msg_id, new_attempts, exc)
    else:
        logger.warning("outbox 消息处理失败 id=%s attempts=%s 下次重试 +%.1fs err=%s",
                       msg_id, new_attempts, delay, exc)
    return "dead" if dead else "failed"


# ────────────────────────── 批量处理 ──────────────────────────
def process_batch(*, limit: int = 0, biz_date: Optional[str] = None,
                  fault: Optional[Callable[[Any], None]] = None,
                  max_attempts: Optional[int] = None,
                  backoff_base: Optional[float] = None) -> Dict[str, int]:
    """处理一批到期的消息，返回计数。

    `biz_date` 可选：只处理 payload 里这一天消息（"生成某天日报"）。
    `fault` 是**故障注入钩子**：传入一个 callable，每条消息处理前调用它，
    它抛异常就等价于"downstream 挂了/worker 处理逻辑炸了"。
    生产环境不传；演练和实验（bench/outbox_fault.py）传。
    """
    limit = int(limit or DEFAULT_BATCH)
    max_attempts = int(max_attempts or DEFAULT_MAX_ATTEMPTS)
    backoff_base = float(DEFAULT_BACKOFF_BASE if backoff_base is None else backoff_base)

    stats = {"scanned": 0, "done": 0, "failed": 0, "dead": 0}
    touched = []

    for _ in range(limit):
        session = SessionLocal()
        try:
            row = session.execute(text("""
                SELECT id, topic, payload, attempts
                  FROM outbox
                 WHERE status = 'pending'
                   AND next_at <= now()
                   AND (CAST(:biz AS TEXT) IS NULL OR payload ->> 'biz_date' = CAST(:biz AS TEXT))
                 ORDER BY next_at, id
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
            """), {"biz": biz_date}).first()

            if row is None:
                break

            stats["scanned"] += 1
            touched.append(int(row.id))
            try:
                if fault is not None:
                    fault(row)                       # 故障注入点
                _apply(session, row)
                session.execute(text("""
                    UPDATE outbox
                       SET status = 'done', attempts = attempts + 1, last_error = ''
                     WHERE id = :id
                """), {"id": int(row.id)})
                session.commit()
                stats["done"] += 1
            except Exception as exc:                 # noqa: BLE001 - 单条消息的失败不能影响整批
                session.rollback()
                outcome = _record_failure(int(row.id), int(row.attempts), exc,
                                          max_attempts=max_attempts,
                                          backoff_base=backoff_base)
                stats["dead" if outcome == "dead" else "failed"] += 1
        finally:
            session.close()

    if touched:
        _refresh_gauges()
    metrics.WORKER_BATCHES.labels(outcome="done" if stats["failed"] == 0 else "partial").inc()
    return stats


def process_cache_invalidations(*, limit: int = 500) -> Dict[str, int]:
    """**批量**消费 `cache_invalidate` 消息：一个事务取一批 → 一次删一批 key → 一起置 done。

    为什么单独一条路径，而不是复用 `process_batch` 的逐条处理：
    缓存失效是**幂等且可批量**的操作（同一个 key 删两遍无害，一批失败整批重试也无害），
    而逐条处理意味着每条消息一次事务往返。实测过：每条消息 ~10ms，消费只有 ~100 条/秒，
    而写入是 ~460 次/秒 —— 积压一路涨，C 方案的窗口被拉长到接近"从不失效"，
    等于把消息驱动的价值全丢了。

    所以：**批处理是这类消息的标配**，不是优化项。
    （汇总类消息不能这么做：每条要按 (日,项目,版本) 重算，语义上就该各自独立。）
    """
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id, payload
              FROM outbox
             WHERE topic = :topic AND status = 'pending' AND next_at <= now()
             ORDER BY next_at, id
             LIMIT :lim
             FOR UPDATE SKIP LOCKED
        """), {"topic": cache.TOPIC_CACHE_INVALIDATE, "lim": int(limit)}).all()

        if not rows:
            return {"messages": 0, "keys": 0}

        keys = set()
        for row in rows:
            payload = dict(row.payload or {})
            keys.update(str(k) for k in (payload.get("keys") or []) if k)

        # 删失败（Redis 挂了）就抛异常回滚：消息保持 pending 等重试，不能假装成功
        if keys and not cache.delete(*keys):
            raise RuntimeError(f"缓存不可用，{len(keys)} 个 key 未失效")

        ids = [int(row.id) for row in rows]
        s.execute(text("""
            UPDATE outbox SET status = 'done', attempts = attempts + 1, last_error = ''
             WHERE id = ANY(CAST(:ids AS BIGINT[]))
        """), {"ids": ids})
        metrics.CACHE_INVALIDATIONS.labels(reason="batch").inc(len(keys))

    return {"messages": len(ids), "keys": len(keys)}


def replay_dead(*, ids: Optional[List[int]] = None, limit: int = 100) -> int:
    """把死信重置成 pending（人工重放）。返回被重放的条数。

    `attempts` 归零：重放是"修好了再来一次"，不该继承上一次的失败预算。
    `last_error` 保留在库里（直到下次处理覆盖），方便事后追。
    """
    with session_scope() as s:
        # PG 的 UPDATE 不支持 LIMIT，改成"先在子查询里挑出 id，再按 id 更新"
        rows = s.execute(text("""
            UPDATE outbox
               SET status = 'pending', attempts = 0, next_at = now()
             WHERE id IN (
                 SELECT id FROM outbox
                  WHERE status = 'dead'
                    AND (CAST(:ids AS BIGINT[]) IS NULL
                         OR id = ANY(CAST(:ids AS BIGINT[])))
                  ORDER BY id
                  LIMIT :lim
             )
            RETURNING id
        """), {"ids": ids, "lim": int(limit)}).all()
    replayed = [int(r.id) for r in rows]
    if replayed:
        logger.warning("人工重放死信 %s 条：%s", len(replayed), replayed[:20])
        _refresh_gauges()
    return len(replayed)


# ────────────────────────── 观测 ──────────────────────────
def outbox_stats() -> Dict[str, Any]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT status, count(*) AS n,
                   min(next_at) AS earliest,
                   max(attempts) AS max_attempts
              FROM outbox GROUP BY status
        """)).all()
        summary_rows = int(s.execute(text(
            "SELECT count(*) FROM daily_exec_summary")).scalar_one())
    by_status = {r.status: int(r.n) for r in rows}
    return {
        "pending": by_status.get("pending", 0),
        "done": by_status.get("done", 0),
        "dead": by_status.get("dead", 0),
        "summary_rows": summary_rows,
        "earliest_due": min((r.earliest for r in rows if r.earliest), default=None),
        "max_attempts_seen": max((int(r.max_attempts or 0) for r in rows), default=0),
    }


def list_messages(*, status: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """看 outbox 里现在有什么（运维/演示用）。

    注意 outbox 表**没有 created_at 列**（见 001_baseline.sql）：`next_at` 插入时就是 now()，
    所以它天然带着"这条消息是什么时候产生的"信息，积压时长可以用 `now() - next_at` 粗看。
    """
    limit = max(1, min(int(limit or 20), 200))
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id, topic, status, attempts, next_at, last_error, payload
              FROM outbox
             WHERE (CAST(:st AS TEXT) IS NULL OR status = CAST(:st AS TEXT))
             ORDER BY id DESC
             LIMIT :lim
        """), {"st": status, "lim": limit}).all()
    return {
        "items": [
            {"id": int(r.id), "topic": r.topic, "status": r.status,
             "attempts": int(r.attempts), "next_at": r.next_at,
             "last_error": r.last_error, "payload": dict(r.payload or {})}
            for r in rows
        ],
        "stats": outbox_stats(),
    }


def diff_summary(*, project_id: Optional[int] = None,
                 biz_date: Optional[str] = None) -> Dict[str, Any]:
    """汇总 vs 明细的差异（outbox 最终一致的验收口径）。

    这只是**数字对账**；第 3 周的 `POST /v1/jobs/recon` 还会加
    `fail_without_defect` 这类业务规则型检查，并落 `recon_discrepancies`。

    按 (日, 项目, 版本) 全外连接：`missing` = 有明细没汇总（少算），
    `extra` = 有汇总没明细（多算/脏数据），`mismatched` = 数字对不上。
    """
    where_d = ["1 = 1"]
    params: Dict[str, Any] = {"pid": project_id, "biz": None}
    if project_id is not None:
        where_d.append("q.project_id = :pid")
    if biz_date:
        day = date_cls.fromisoformat(biz_date)
        start, end = biz_day_bounds(day)
        where_d.append("e.executed_at >= :start AND e.executed_at < :end")
        params.update({"biz": day, "start": start, "end": end})

    # 明细侧也按本地自然日归集：和 worker 写汇总用的是同一个口径（biz_date_sql）
    biz_expr = biz_date_sql("e.executed_at")
    sql = f"""
        WITH detail AS (
            SELECT q.project_id,
                   q.version_id,
                   {biz_expr} AS biz_date,
                   count(*)                                     AS total,
                   count(*) FILTER (WHERE e.result = 'pass')     AS passed,
                   count(*) FILTER (WHERE e.result = 'fail')     AS failed,
                   count(*) FILTER (WHERE e.result = 'blocked')  AS blocked
              FROM executions e
              JOIN execution_queue q ON q.id = e.queue_id
             WHERE {' AND '.join(where_d)}
             GROUP BY q.project_id, q.version_id, {biz_expr}
        )
        SELECT COALESCE(d.biz_date, s.biz_date)     AS biz_date,
               COALESCE(d.project_id, s.project_id) AS project_id,
               COALESCE(d.version_id, s.version_id) AS version_id,
               d.total   AS detail_total,   s.total   AS summary_total,
               d.passed  AS detail_passed,  s.passed  AS summary_passed,
               d.failed  AS detail_failed,  s.failed  AS summary_failed,
               d.blocked AS detail_blocked, s.blocked AS summary_blocked
          FROM detail d
          FULL JOIN daily_exec_summary s
                 ON s.biz_date   = d.biz_date
                AND s.project_id = d.project_id
                AND s.version_id = d.version_id
         WHERE (CAST(:pid AS BIGINT) IS NULL
                 OR COALESCE(d.project_id, s.project_id) = CAST(:pid AS BIGINT))
           AND (CAST(:biz AS DATE) IS NULL
                 OR COALESCE(d.biz_date, s.biz_date) = CAST(:biz AS DATE))
    """
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()

    mismatched, missing, extra = [], [], []
    for r in rows:
        detail_total = int(r.detail_total or 0)
        summary_total = int(r.summary_total or 0)
        entry = {
            "biz_date": str(r.biz_date), "project_id": int(r.project_id),
            "version_id": int(r.version_id),
            "detail": {"total": detail_total, "passed": int(r.detail_passed or 0),
                       "failed": int(r.detail_failed or 0),
                       "blocked": int(r.detail_blocked or 0)},
            "summary": {"total": summary_total, "passed": int(r.summary_passed or 0),
                        "failed": int(r.summary_failed or 0),
                        "blocked": int(r.summary_blocked or 0)},
        }
        if r.summary_total is None:
            missing.append(entry)          # 有明细没汇总 → 少算
        elif r.detail_total is None:
            extra.append(entry)            # 有汇总没明细 → 多算/脏数据
        elif detail_total != summary_total or entry["detail"] != entry["summary"]:
            mismatched.append(entry)

    return {
        "mismatched": mismatched,
        "missing": missing,
        "extra": extra,
        "diff_count": len(mismatched) + len(missing) + len(extra),
    }


def _refresh_gauges() -> None:
    try:
        stats = outbox_stats()
    except Exception:                                # noqa: BLE001 - 打点失败不能影响业务
        return
    metrics.OUTBOX_PENDING.set(stats["pending"])
    metrics.OUTBOX_DEAD.set(stats["dead"])
