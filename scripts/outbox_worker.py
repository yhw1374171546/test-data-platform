"""outbox worker：常驻轮询 outbox，把执行结果汇总进 `daily_exec_summary`。

用法：

    python scripts/outbox_worker.py --once                 # 处理一轮就退出（演示/CI）
    python scripts/outbox_worker.py --drain                # 一直处理到没有到期消息
    python scripts/outbox_worker.py                        # 常驻，默认每 2 秒一轮
    python scripts/outbox_worker.py --once --inject-failure # 演练：模拟 downstream 挂掉

退出码：0 正常；1 存在死信（方便在 CI/巡检里直接当告警用）。

为什么 worker 是独立进程而不是 app 里的后台线程：
  ① 可以独立扩缩容（消息多了多开几个 worker，`SKIP LOCKED` 保证不重复处理）；
  ② 进程崩了不影响 API；
  ③ 部署上能单独看它的日志和指标。
后台线程版本最容易出的问题是"API 一重启，积压就没人处理了"，而且排查时日志混在一起。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from typing import Any, Callable, Dict, Optional

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.service.outbox import (DEFAULT_BACKOFF_BASE, DEFAULT_MAX_ATTEMPTS,
                                outbox_stats, process_batch)


def _injected_fault(_row: Any) -> None:
    """故障注入：等价于"汇总写不进去"（downstream 挂了 / DB 拒绝写入）。"""
    raise RuntimeError("注入故障：模拟 downstream 不可用")


def _print_round(index: int, stats: Dict[str, int], remaining: Dict[str, Any]) -> None:
    print(f"[worker] 第 {index} 轮  scanned={stats['scanned']} done={stats['done']} "
          f"failed={stats['failed']} dead={stats['dead']}  "
          f"| 剩余 pending={remaining['pending']} dead={remaining['dead']}")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="outbox worker：执行结果 → 日报汇总")
    ap.add_argument("--once", action="store_true", help="只处理一轮")
    ap.add_argument("--drain", action="store_true", help="处理到没有到期消息为止")
    ap.add_argument("--interval", type=float, default=2.0, help="常驻模式每轮间隔秒数")
    ap.add_argument("--batch", type=int, default=50, help="每轮最多处理多少条")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF_BASE,
                    help="指数退避基数（秒），第 n 次失败等 base*2^(n-1)")
    ap.add_argument("--biz-date", default=None, help="只处理这一天的消息（YYYY-MM-DD）")
    ap.add_argument("--inject-failure", action="store_true", help="故障注入（演练用）")
    ap.add_argument("--strict", action="store_true",
                    help="有死信时以退出码 1 结束（给 CI/巡检用；交互式演示默认 0）")
    ap.add_argument("--max-rounds", type=int, default=10_000, help="--drain 的最大轮数保护")
    args = ap.parse_args(argv)

    fault: Optional[Callable[[Any], None]] = _injected_fault if args.inject_failure else None
    if args.inject_failure:
        print("[worker] 已开启故障注入：本轮所有消息都会处理失败（演练模式）")

    rounds = 0
    totals = {"scanned": 0, "done": 0, "failed": 0, "dead": 0}

    try:
        while rounds < args.max_rounds:
            rounds += 1
            stats = process_batch(limit=args.batch, biz_date=args.biz_date,
                                  fault=fault, max_attempts=args.max_attempts,
                                  backoff_base=args.backoff)
            for key in totals:
                totals[key] += stats[key]
            remaining = outbox_stats()
            _print_round(rounds, stats, remaining)

            if args.once or args.drain:
                # drain 模式下，如果这一轮什么都没扫到（或全都失败了）就停：
                # 失败的消息已经被排到未来，继续空转没有意义。
                if stats["scanned"] == 0 or stats["done"] == 0:
                    break
            if not args.once and not args.drain:
                time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:                       # pragma: no cover - 交互式
        print("\n[worker] 收到中断，退出")

    final = outbox_stats()
    print("=" * 68)
    print(f"[worker] 结束：处理 {totals['scanned']} 条（done={totals['done']} "
          f"failed={totals['failed']} dead={totals['dead']}）")
    print(f"[worker] 当前积压：pending={final['pending']} dead={final['dead']} "
          f"汇总行数={final['summary_rows']}")

    if final["dead"]:
        print(f"[worker] 有 {final['dead']} 条死信，需要告警 + 人工重放：\n"
              f"         POST /v1/admin/outbox/replay    （或 `dev.cmd worker` 前先重放）")
        if args.strict:
            return 1
        print("[worker] 交互模式默认退出码 0；CI/巡检请加 --strict")
    return 0


if __name__ == "__main__":
    sys.exit(main())
