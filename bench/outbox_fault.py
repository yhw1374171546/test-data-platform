"""outbox 故障注入实验：**worker 一直失败 → 死信 → 重放 → 最终一致**（技术方案 §4.4）。

这是一条"能不能真的收敛"的实验，不是演示脚本。三幕：

  第一幕 注入故障：worker 每轮都在 `_apply` 之前抛异常，等价于"汇总写不进去"。
          观察 attempts 累加、指数退避、超过 max_attempts 置 dead。
  第二幕 只恢复、不重放：worker 正常了，但死信不会自己复活 ——
          **差异仍然存在**。这一幕最容易被忽略：很多人以为"故障恢复就自动一致了"。
  第三幕 人工重放死信：差异归零。

每一幕都用 `drain()` 反复跑到"没有 pending 消息"为止（中间会按退避窗口 sleep），
也就是真实 worker `--drain` 的行为 —— 而不是一次性跑一轮就下结论。

用法：
    python bench/outbox_fault.py                     # 20 条消息、max_attempts=3
    python bench/outbox_fault.py --messages 50 --max-attempts 5 --backoff 1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                                  # noqa: E402

from app.domain.biz_time import TZ                           # noqa: E402
from app.infra.db import session_scope                       # noqa: E402
from app.service.claim import claim_task                     # noqa: E402
from app.service.execution import submit_execution           # noqa: E402
from app.service.outbox import (diff_summary, process_batch,  # noqa: E402
                                replay_dead)

# worker 的失败日志在这里是"预期的噪声"：本实验就是要制造失败。
# 把 tdp.outbox 的日志压掉，输出才看得清（生产环境千万别这么干）。
logging.getLogger("tdp.outbox").setLevel(logging.CRITICAL)


def seed(messages: int) -> Dict[str, Any]:
    """一个项目 + 一个版本 + N 条任务，全部领取并提交结果（产生 N 条 outbox 消息）。"""
    tag = uuid.uuid4().hex[:8]
    day = date(2024, 1, 1) + timedelta(days=uuid.uuid4().int % 700)
    at = datetime.combine(day, dtime(10, 0), tzinfo=TZ)

    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '故障注入项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-OUTBOX-{tag}"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-BENCH-{tag}"}).scalar_one()
        tester_id = s.execute(text("""
            INSERT INTO testers (employee_no, name, team)
            VALUES (:e, '压测员', '压测组') RETURNING id
        """), {"e": f"EB-{tag}"}).scalar_one()

        task_ids: List[int] = []
        for i in range(messages):
            cid = s.execute(text("""
                INSERT INTO test_cases (case_no, module, title)
                VALUES (:c, '领航', :t) RETURNING id
            """), {"c": f"TC-BENCH-{tag}-{i}", "t": f"故障注入用例 {i}"}).scalar_one()
            task_ids.append(int(s.execute(text("""
                INSERT INTO execution_queue (project_id, version_id, case_id)
                VALUES (:p, :v, :c) RETURNING id
            """), {"p": pid, "v": vid, "c": cid}).scalar_one()))

    for i, task_id in enumerate(task_ids):
        claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                   tester_id=int(tester_id))
        submit_execution(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                         tester_id=int(tester_id),
                         result=("pass", "fail", "blocked", "skip")[i % 4],
                         executed_at=at)

    return {"project_id": int(pid), "version_id": int(vid),
            "tester_id": int(tester_id), "day": day.isoformat(), "messages": messages}


def outbox_view(project_id: int) -> Dict[str, int]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT count(*)                                   AS total,
                   count(*) FILTER (WHERE status = 'done')    AS done,
                   count(*) FILTER (WHERE status = 'dead')    AS dead,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   coalesce(sum(attempts), 0)                 AS attempts_sum,
                   coalesce(max(attempts), 0)                 AS attempts_max
              FROM outbox WHERE payload ->> 'project_id' = :pid
        """), {"pid": str(project_id)}).one()
    return {"total": int(row.total), "done": int(row.done), "dead": int(row.dead),
            "pending": int(row.pending), "attempts_sum": int(row.attempts_sum),
            "attempts_max": int(row.attempts_max)}


def dead_ids(project_id: int) -> List[int]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id FROM outbox
             WHERE status = 'dead' AND payload ->> 'project_id' = :pid
             ORDER BY id
        """), {"pid": str(project_id)}).all()
    return [int(r.id) for r in rows]


def drain(project_id: int, day: str, *, fault=None, max_attempts: int,
          backoff: float, max_rounds: int = 200) -> Dict[str, int]:
    """像真实 worker 一样反复跑，直到没有 pending 消息（或轮数用完）。"""
    totals = {"scanned": 0, "done": 0, "failed": 0, "dead": 0, "rounds": 0}
    for _ in range(max_rounds):
        stats = process_batch(limit=200, biz_date=day, fault=fault,
                              max_attempts=max_attempts, backoff_base=backoff)
        totals["rounds"] += 1
        for key in ("scanned", "done", "failed", "dead"):
            totals[key] += stats[key]
        if outbox_view(project_id)["pending"] == 0:
            break
        if stats["scanned"] == 0:
            time.sleep(backoff)              # 所有消息都在退避窗口里，等一下再轮询
    return totals


def inject(_row: Any) -> None:
    raise RuntimeError("注入故障：汇总写不进去")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=20)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=0.3,
                    help="退避基数（秒）。可容忍故障时长 ≈ base*(2^n-1)")
    args = ap.parse_args()

    print("=" * 74)
    print(f"outbox 故障注入 · {args.messages} 条消息 · max_attempts={args.max_attempts} "
          f"· backoff={args.backoff}s")
    print("=" * 74)

    env = seed(args.messages)
    pid, day = env["project_id"], env["day"]
    tolerance = args.backoff * (2 ** args.max_attempts - 1)
    print(f"\n已提交 {env['messages']} 条执行结果（业务日 {day}），产生同样条数的 outbox 消息")
    print(f"退避预算：{args.backoff}s × (2^{args.max_attempts}-1) ≈ {tolerance:.1f}s "
          f"—— 故障超过这个时长，消息就会被打成死信")

    failed = 0

    # ── 第一幕：注入故障 ──
    started = time.perf_counter()
    first = drain(pid, day, fault=inject, max_attempts=args.max_attempts,
                  backoff=args.backoff)
    elapsed1 = time.perf_counter() - started
    view1 = outbox_view(pid)

    print(f"\n[第一幕] 注入故障：跑到没有 pending 为止（{first['rounds']} 轮 / {elapsed1:.1f}s）")
    print(f"    outbox：done={view1['done']} dead={view1['dead']} pending={view1['pending']}")
    print(f"    重试次数：合计 {view1['attempts_sum']} 次，单条最多 {view1['attempts_max']} 次"
          f"（理论 {args.max_attempts} 次后置 dead）")
    ok1 = view1["pending"] == 0 and view1["dead"] == view1["total"] \
        and view1["attempts_max"] == args.max_attempts
    print(f"    {'[OK]' if ok1 else '[FAIL]'} 重试预算耗尽 → 全部进入死信（不丢弃、不静默）")
    failed += 0 if ok1 else 1

    # ── 第二幕：只恢复，不重放 ──
    second = drain(pid, day, max_attempts=args.max_attempts, backoff=args.backoff)
    diff2 = diff_summary(project_id=pid)
    missing_detail = sum(e["detail"]["total"] for e in diff2["missing"])
    print("\n[第二幕] 故障恢复，但**不重放**死信")
    print(f"    本轮处理：{second['scanned']} 条（死信不会被自动捡起）")
    print(f"    汇总差异：{diff2['diff_count']} 个 (日,项目,版本) 有偏差，"
          f"其中未汇总的明细 {missing_detail} 条")
    ok2 = second["scanned"] == 0 and diff2["diff_count"] > 0
    print(f"    {'[OK]' if ok2 else '[FAIL]'} 没有重放就不会一致 —— 死信必须告警 + 人工处理")
    failed += 0 if ok2 else 1

    # ── 第三幕：重放 + 再处理 ──
    ids = dead_ids(pid)
    replayed = replay_dead(ids=ids, limit=len(ids) or 1)
    third = drain(pid, day, max_attempts=args.max_attempts, backoff=args.backoff)
    diff3 = diff_summary(project_id=pid)
    view3 = outbox_view(pid)
    print(f"\n[第三幕] 人工重放 {replayed} 条死信后重新处理（{third['rounds']} 轮）")
    print(f"    本轮处理：done={third['done']} failed={third['failed']} dead={third['dead']}")
    print(f"    outbox：done={view3['done']} dead={view3['dead']} pending={view3['pending']}")
    print(f"    汇总差异：{diff3['diff_count']}")
    ok3 = diff3["diff_count"] == 0 and view3["done"] == view3["total"]
    print(f"    {'[OK]' if ok3 else '[FAIL]'} 最终一致：汇总 == 明细")
    failed += 0 if ok3 else 1

    with session_scope() as s:
        summary = s.execute(text("""
            SELECT biz_date, total, passed, failed, blocked FROM daily_exec_summary
             WHERE project_id = :pid ORDER BY biz_date LIMIT 1
        """), {"pid": pid}).one_or_none()
    if summary is not None:
        print(f"\n[汇总行] {summary.biz_date}  total={summary.total} "
              f"passed={summary.passed} failed={summary.failed} blocked={summary.blocked}")
        skipped = int(summary.total) - (int(summary.passed) + int(summary.failed)
                                        + int(summary.blocked))
        print(f"         skip={skipped}（只进 total：汇总表没有 skipped 列，这是既定口径）")

    print("\n" + "=" * 74)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
