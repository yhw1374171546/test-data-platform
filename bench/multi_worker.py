"""多 worker 实测：N 个 worker 进程同时消费 outbox，验证**不重复处理**。

为什么单独做这个实验：
`FOR UPDATE SKIP LOCKED` 写起来只有一行，但它到底能不能撑住"多副本"是被问得最多的问题。
单进程跑通不代表多进程没问题 —— 复制粘贴一份代码就是两个 worker 了，必须实测。

判定"没有重复处理"的方法：
每条消息成功时 `attempts` 只会 +1。如果两个 worker 处理了同一条消息，
它必然出现 attempts >= 2。所以断言 **所有 done 消息的 attempts 都等于 1** +
汇总表的 total 恰好等于消息数（不是 2 倍）。

用法：
    python bench/multi_worker.py --workers 3 --messages 60
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import text                                  # noqa: E402

from app.domain.biz_time import TZ                           # noqa: E402
from app.infra.db import session_scope                       # noqa: E402
from app.service.claim import claim_task                     # noqa: E402
from app.service.execution import submit_execution           # noqa: E402
from app.service.outbox import diff_summary                  # noqa: E402

RESULTS = ("pass", "fail", "blocked", "skip")


def seed(messages: int, label: str) -> Dict[str, Any]:
    """一个项目 + N 条任务，全部领取并提交结果（产生 N 条 outbox 消息）。"""
    tag = uuid.uuid4().hex[:8]
    day = date(2024, 1, 1) + timedelta(days=uuid.uuid4().int % 700)
    at = datetime.combine(day, dtime(10, 0), tzinfo=TZ)

    with session_scope() as s:
        pid = int(s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, :n, 'U8')
            RETURNING id
        """), {"c": f"BENCH-MW-{label}-{tag}", "n": f"多 worker 实验（{label}）"}).scalar_one())
        vid = int(s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-MW-{tag}"}).scalar_one())
        tester_id = int(s.execute(text("""
            INSERT INTO testers (employee_no, name, team)
            VALUES (:e, '压测员', '压测组') RETURNING id
        """), {"e": f"EMW-{tag}"}).scalar_one())

        task_ids: List[int] = []
        for i in range(messages):
            cid = int(s.execute(text("""
                INSERT INTO test_cases (case_no, module, title)
                VALUES (:c, '领航', :t) RETURNING id
            """), {"c": f"TC-MW-{tag}-{i}", "t": f"多 worker 用例 {i}"}).scalar_one())
            task_ids.append(int(s.execute(text("""
                INSERT INTO execution_queue (project_id, version_id, case_id)
                VALUES (:p, :v, :c) RETURNING id
            """), {"p": pid, "v": vid, "c": cid}).scalar_one()))

    for i, task_id in enumerate(task_ids):
        claim_task(idem_key=f"mw-{uuid.uuid4().hex}", task_id=task_id, tester_id=tester_id)
        submit_execution(idem_key=f"mw-{uuid.uuid4().hex}", task_id=task_id,
                         tester_id=tester_id, result=RESULTS[i % len(RESULTS)],
                         executed_at=at)

    expected: Dict[str, int] = {r: messages // len(RESULTS) for r in RESULTS}
    for i in range(messages % len(RESULTS)):
        expected[RESULTS[i]] += 1

    return {"project_id": pid, "version_id": vid, "tester_id": tester_id,
            "day": day, "messages": messages, "expected": expected}


def run_workers(count: int, *, max_rounds: int = 500) -> Dict[str, Any]:
    """同时拉起 `count` 个 worker 子进程，等它们各自 drain 完。"""
    env = dict(os.environ)
    # 每个 worker 一个进程，各带一个连接池：默认 20+20 会让 3 个 worker 就把 PG 的连接打满
    env["TDP_POOL_SIZE"] = "5"
    env["TDP_POOL_OVERFLOW"] = "5"
    env["PYTHONUTF8"] = "1"

    started = time.perf_counter()
    procs = [
        subprocess.Popen(
            [sys.executable, "scripts/outbox_worker.py",
             "--drain", "--max-rounds", str(max_rounds)],
            cwd=str(BASE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for _ in range(count)
    ]
    outputs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=600)
        outputs.append(out or "")
    elapsed = time.perf_counter() - started

    return {"workers": count, "seconds": elapsed, "outputs": outputs,
            "exit_codes": [p.returncode for p in procs]}


def outbox_stats(project_id: int) -> Dict[str, int]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT count(*)                                       AS total,
                   count(*) FILTER (WHERE status = 'done')        AS done,
                   count(*) FILTER (WHERE status = 'pending')     AS pending,
                   count(*) FILTER (WHERE status = 'dead')        AS dead,
                   coalesce(max(attempts), 0)                     AS attempts_max,
                   count(*) FILTER (WHERE attempts <> 1)          AS not_once
              FROM outbox WHERE payload ->> 'project_id' = :pid
        """), {"pid": str(project_id)}).one()
    return {k: int(v) for k, v in row._mapping.items()}


def summary_row(project_id: int, version_id: int, day: date) -> Dict[str, Any]:
    """daily_exec_summary 只有 total/passed/failed/blocked —— `skip` 不单独存，
    但**要算进 total**（否则"跑了 40 条"和"通过+失败+阻塞"对不上，对账会误报）。"""
    with session_scope() as s:
        row = s.execute(text("""
            SELECT total, passed, failed, blocked
              FROM daily_exec_summary
             WHERE project_id = :p AND version_id = :v AND biz_date = :d
        """), {"p": project_id, "v": version_id, "d": day}).first()
    return dict(row._mapping) if row else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--messages", type=int, default=60)
    args = ap.parse_args()

    if args.messages < args.workers * 4:
        print("消息太少，测不出并发协作：建议 messages >= workers * 4")
        return 2

    print("=" * 78)
    print(f"多 worker 实测 · {args.workers} 个进程 × {args.messages} 条消息")
    print("=" * 78)

    # ── 对照组：单 worker 跑同样多的消息，用来算并行加速 ──
    single = seed(args.messages, "single")
    print(f"造数完成（对照组）：{args.messages} 条消息")
    one = run_workers(1)
    one_stats = outbox_stats(single["project_id"])
    print(f"  单 worker：{one['seconds']:.2f}s，done={one_stats['done']}，"
          f"attempts_max={one_stats['attempts_max']}")

    # ── 实验组：N 个 worker 同时抢 ──
    multi = seed(args.messages, f"{args.workers}w")
    print(f"造数完成（实验组）：{args.messages} 条消息")
    many = run_workers(args.workers)
    many_stats = outbox_stats(multi["project_id"])
    summary = summary_row(multi["project_id"], multi["version_id"], multi["day"])
    diffs = diff_summary(biz_date=str(multi["day"]), project_id=multi["project_id"])
    print(f"  {args.workers} 个 worker：{many['seconds']:.2f}s，done={many_stats['done']}，"
          f"attempts_max={many_stats['attempts_max']}")

    speedup = one["seconds"] / many["seconds"] if many["seconds"] else 0.0
    print(f"\n  并行加速：{speedup:.2f}x（{one['seconds']:.2f}s → {many['seconds']:.2f}s）")

    print("\n" + "=" * 78)
    print("校验")
    print("=" * 78)
    failed = 0
    checks = [
        (f"{args.workers} 个 worker 全部正常退出",
         all(code == 0 for code in many["exit_codes"]), f"exit={many['exit_codes']}"),
        (f"所有 {args.messages} 条消息都处理完了",
         many_stats["done"] == args.messages and many_stats["pending"] == 0,
         f"done={many_stats['done']} pending={many_stats['pending']}"),
        ("没有死信", many_stats["dead"] == 0, f"dead={many_stats['dead']}"),
        ("**没有重复处理**（每条消息 attempts 恰好 1 次）",
         many_stats["not_once"] == 0, f"attempts<>1 的消息数={many_stats['not_once']}"),
        ("汇总 total 恰好等于消息数（没有被多算）",
         summary.get("total") == args.messages, f"total={summary.get('total')}"),
        ("汇总 passed/failed/blocked 与预期一致",
         all(summary.get(col) == multi["expected"][key]
             for col, key in (("passed", "pass"), ("failed", "fail"), ("blocked", "blocked"))),
         f"实际={ {c: summary.get(c) for c in ('passed', 'failed', 'blocked')} } "
         f"预期={ {k: multi['expected'][k] for k in ('pass', 'fail', 'blocked')} }"),
        ("skip 被算进 total（total - 三者之和 == skip）",
         summary.get("total", 0) - sum(summary.get(k, 0)
                                      for k in ("passed", "failed", "blocked"))
         == multi["expected"]["skip"],
         f"推算 skip={summary.get('total', 0) - sum(summary.get(k, 0) for k in ('passed', 'failed', 'blocked'))}"
         f" 预期={multi['expected']['skip']}"),
        ("明细与汇总无差异", diffs["diff_count"] == 0, f"diff_count={diffs['diff_count']}"),
    ]
    for label, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}：{detail}")
        failed += 0 if ok else 1

    print(f"\n  单 worker 用时 {one['seconds']:.2f}s，{args.workers} 个 worker 用时 "
          f"{many['seconds']:.2f}s。加速比不一定是 {args.workers}x —— 这批消息很小，")
    print("  每个 worker 都花了不少时间在启动和抢连接上；但**正确性**必须是 1x：")
    print("  没有一条消息被两个进程处理两次，汇总也没有被重复累加。")

    print("\n" + "=" * 78)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
