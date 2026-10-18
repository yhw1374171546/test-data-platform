"""提交执行结果竞争压测：验证「一次提交只落一次库」（技术方案 §4.4 的原子性部分）。

两组实验：

  A. N 个线程 × N 个**不同**幂等键提交同一条任务
     -> recorded=1，其余 409 already_submitted；executions=1、outbox=1
  B. N 个线程 × **同一个**幂等键并发提交
     -> recorded=1、replayed=N-1，且所有重放返回同一个 execution_id；executions=1、outbox=1

为什么两组都要：
  · A 考的是 `UPDATE ... WHERE status IN ('claimed','running') AND claimed_by=?` 的原子性
  · B 考的是幂等键。只有 A 会漏掉"客户端超时重试"这个最常见的真实场景。
而"executions 和 outbox 都只有一条"是两组共同的硬指标：
  多一条明细 → 日报多算；多一条 outbox → 汇总再叠一次，等于同一份结果被统计两遍。

用法：
    python bench/execution_submit.py --concurrency 20
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid
from collections import Counter
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                                  # noqa: E402

from app.infra.db import session_scope                       # noqa: E402
from app.service.claim import QueueConflict, claim_task      # noqa: E402
from app.service.execution import submit_execution           # noqa: E402


def seed() -> Dict[str, Any]:
    """一条 pending 任务 + 一个测试员，实验开始前先领取（提交结果的前提）。"""
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '压测项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-EXEC-{tag}"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-BENCH-{tag}"}).scalar_one()
        cid = s.execute(text("""
            INSERT INTO test_cases (case_no, module, title)
            VALUES (:c, '领航', '提交结果压测用例') RETURNING id
        """), {"c": f"TC-BENCH-{tag}"}).scalar_one()
        task_id = s.execute(text("""
            INSERT INTO execution_queue (project_id, version_id, case_id)
            VALUES (:p, :v, :c) RETURNING id
        """), {"p": pid, "v": vid, "c": cid}).scalar_one()
        tester_id = s.execute(text("""
            INSERT INTO testers (employee_no, name, team)
            VALUES (:e, '压测员', '压测组') RETURNING id
        """), {"e": f"EB-{tag}"}).scalar_one()

    claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=int(task_id),
               tester_id=int(tester_id))
    return {"task_id": int(task_id), "tester_id": int(tester_id)}


def run_race(concurrency: int, task_id: int, tester_id: int,
             same_key: bool) -> Counter:
    results: Counter = Counter()
    execution_ids: List[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency, timeout=30)
    shared_key = f"bench-{uuid.uuid4().hex}"

    def worker(i: int) -> None:
        key = shared_key if same_key else f"bench-{uuid.uuid4().hex}"
        # 同 key 组必须发**完全一样**的 body：result 交替会被判 "同 key 换 body"(422)。
        # 这不是实现问题，正是幂等键该有的行为——所以这里让 body 固定。
        result = "pass" if (same_key or i % 2 == 0) else "fail"
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            res = submit_execution(idem_key=key, task_id=task_id, tester_id=tester_id,
                                   result=result, note="压测")
            tag, execution_id = res.code, res.execution_id
        except QueueConflict as exc:
            tag, execution_id = exc.code, None
        except Exception as exc:                        # noqa: BLE001
            tag, execution_id = f"error:{type(exc).__name__}", None
        with lock:
            results[tag] += 1
            if execution_id:
                execution_ids.append(int(execution_id))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results["_execution_ids"] = len(set(execution_ids))   # type: ignore[assignment]
    return results


def counts(task_id: int) -> Dict[str, int]:
    with session_scope() as s:
        executions = int(s.execute(text(
            "SELECT count(*) FROM executions WHERE queue_id = :q"),
            {"q": task_id}).scalar_one())
        outbox = int(s.execute(text(
            "SELECT count(*) FROM outbox WHERE payload ->> 'queue_id' = :q"),
            {"q": str(task_id)}).scalar_one())
        row = s.execute(text(
            "SELECT status, version FROM execution_queue WHERE id = :q"),
            {"q": task_id}).one()
    return {"executions": executions, "outbox": outbox,
            "task_status": row.status, "task_version": int(row.version)}


def outbox_sample(task_id: int) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT topic, status, attempts, payload FROM outbox
             WHERE payload ->> 'queue_id' = :q LIMIT 1
        """), {"q": str(task_id)}).one()
    return {"topic": row.topic, "status": row.status,
            "attempts": int(row.attempts), "payload": dict(row.payload)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()
    n = args.concurrency

    print("=" * 74)
    print(f"提交执行结果竞争压测 · 并发 {n}")
    print("=" * 74)

    failed = 0

    # ── A. 不同幂等键 ──
    env_a = seed()
    started = time.perf_counter()
    res_a = run_race(n, env_a["task_id"], env_a["tester_id"], same_key=False)
    ms_a = (time.perf_counter() - started) * 1000
    state_a = counts(env_a["task_id"])
    ok_a = (res_a.get("recorded", 0) == 1
            and res_a.get("already_submitted", 0) == n - 1
            and state_a["executions"] == 1 and state_a["outbox"] == 1
            and state_a["task_status"] == "done")
    print(f"\n[A] {n} 个**不同**幂等键并发提交同一条任务")
    print(f"    结果分布: { {k: v for k, v in res_a.items() if not k.startswith('_')} }")
    print(f"    落库：executions={state_a['executions']} outbox={state_a['outbox']} "
          f"task={state_a['task_status']} v{state_a['task_version']}")
    print(f"    耗时(整批墙钟): {ms_a:.0f} ms")
    print(f"    {'[OK]' if ok_a else '[FAIL]'} 恰好 1 次落库，明细与 outbox 各 1 条")
    failed += 0 if ok_a else 1

    # ── B. 同一幂等键 ──
    env_b = seed()
    started = time.perf_counter()
    res_b = run_race(n, env_b["task_id"], env_b["tester_id"], same_key=True)
    ms_b = (time.perf_counter() - started) * 1000
    state_b = counts(env_b["task_id"])
    ok_b = (res_b.get("recorded", 0) == 1
            and res_b.get("replayed", 0) == n - 1
            and res_b.get("_execution_ids") == 1
            and state_b["executions"] == 1 and state_b["outbox"] == 1)
    print(f"\n[B] 同 1 个**相同**幂等键并发提交 {n} 次")
    print(f"    结果分布: { {k: v for k, v in res_b.items() if not k.startswith('_')} }")
    print(f"    重放返回的不同 execution_id 个数: {res_b.get('_execution_ids')}（要求 1）")
    print(f"    落库：executions={state_b['executions']} outbox={state_b['outbox']}")
    print(f"    耗时(整批墙钟): {ms_b:.0f} ms")
    print(f"    {'[OK]' if ok_b else '[FAIL]'} 幂等生效：1 次真实写入 + {n - 1} 次重放")
    failed += 0 if ok_b else 1

    sample = outbox_sample(env_a["task_id"])
    print("\n[C] outbox 消息样本（第 3 周 worker 的输入）")
    print(f"    topic={sample['topic']} status={sample['status']} attempts={sample['attempts']}")
    print(f"    payload={sample['payload']}")
    has_biz = bool(sample["payload"].get("biz_date"))
    print(f"    {'[OK]' if has_biz else '[FAIL]'} payload 带 biz_date（对账按本地自然日归集）")
    failed += 0 if has_biz else 1

    print("\n" + "=" * 74)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
