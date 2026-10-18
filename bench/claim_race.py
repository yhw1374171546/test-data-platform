"""任务领取竞争压测：验证「乐观锁抢占 + 幂等」在真并发下的正确性（技术方案 §4.2）。

四组实验，每组都要出数：

  A. N 个测试员、N 个**不同**幂等键抢同一条任务
     -> 恰好 1 人 claimed，其余 already_claimed；version 只 +1
  B. 同一个测试员、**同一个**幂等键并发 N 次
     -> 1 次 claimed、N-1 次 replayed，且所有重放返回同一个版本号/领取时间
  C. 同 key 换 body（换一个测试员）
     -> 422 body_mismatch，且落库的 claimed_by 仍是第一个人
  D. 领取 -> 释放 -> 拿**旧版本号**重领 -> 新版本号重领
     -> 旧版本 409 version_conflict（ABA 保护），新版本领取成功

为什么 D 必须单独跑：
如果只验 A/B/C，`version` 看起来是"可有可无"——status='pending' 已经够用了。
D 才证明乐观锁真正防的东西：**任务被领取又释放后，陈旧请求不能假装自己领到了**。

用法：
    python bench/claim_race.py --concurrency 20
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
from app.service.claim import (QueueConflict, claim_task,    # noqa: E402
                               release_task)


def seed(testers_count: int) -> Dict[str, Any]:
    """准备：一个项目 + 一个版本 + 一条用例 + 一条 pending 任务 + N 个测试员。

    每组实验都用全新的任务，避免上一轮的领取状态污染下一轮。
    """
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '压测项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-CLAIM-{tag}"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-BENCH-{tag}"}).scalar_one()
        cid = s.execute(text("""
            INSERT INTO test_cases (case_no, module, title)
            VALUES (:c, '领航', '领取竞争压测用例') RETURNING id
        """), {"c": f"TC-BENCH-{tag}"}).scalar_one()
        task_id = s.execute(text("""
            INSERT INTO execution_queue (project_id, version_id, case_id)
            VALUES (:p, :v, :c) RETURNING id
        """), {"p": pid, "v": vid, "c": cid}).scalar_one()

        testers: List[int] = []
        for i in range(testers_count):
            testers.append(s.execute(text("""
                INSERT INTO testers (employee_no, name, team)
                VALUES (:e, :n, '压测组') RETURNING id
            """), {"e": f"EB-{tag}-{i}", "n": f"压测员{i}"}).scalar_one())

    return {"project_id": int(pid), "version_id": int(vid), "case_id": int(cid),
            "task_id": int(task_id), "testers": [int(t) for t in testers]}


def run_race(concurrency: int, task_id: int, testers: List[int],
             same_key: bool) -> Counter:
    """并发领取。same_key=True 时所有线程共用一个幂等键（模拟同一个人疯狂重试）。"""
    results: Counter = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency, timeout=30)
    shared_key = f"bench-{uuid.uuid4().hex}"

    def worker(i: int) -> None:
        tester = testers[0] if same_key else testers[i]
        key = shared_key if same_key else f"bench-{uuid.uuid4().hex}"
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            tag = claim_task(idem_key=key, task_id=task_id, tester_id=tester).code
        except QueueConflict as exc:
            tag = exc.code
        except Exception as exc:                        # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        with lock:
            results[tag] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def task_state(task_id: int) -> Any:
    with session_scope() as s:
        return s.execute(text("""
            SELECT status, claimed_by, claimed_at, version
              FROM execution_queue WHERE id = :id
        """), {"id": task_id}).one()


def timed(fn) -> tuple:
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20,
                    help="并发线程数（技术方案验收线是 20）")
    args = ap.parse_args()
    n = args.concurrency

    print("=" * 74)
    print(f"任务领取竞争压测 · 并发 {n}")
    print("=" * 74)

    failed = 0

    # ── A. 不同幂等键抢同一任务 ──
    env_a = seed(n)
    res_a, ms_a = timed(lambda: run_race(n, env_a["task_id"], env_a["testers"], same_key=False))
    state_a = task_state(env_a["task_id"])
    ok_a = (res_a.get("claimed", 0) == 1
            and res_a.get("already_claimed", 0) == n - 1
            and state_a.status == "claimed" and int(state_a.version) == 1)
    print(f"\n[A] {n} 个测试员 × {n} 个**不同**幂等键抢同一任务")
    print(f"    结果分布: {dict(res_a)}")
    print(f"    落库状态: status={state_a.status} version={state_a.version} "
          f"claimed_by={state_a.claimed_by}")
    print(f"    耗时(整批墙钟): {ms_a:.0f} ms")
    print(f"    {'[OK]' if ok_a else '[FAIL]'} 恰好 1 人领到，version 只 +1")
    failed += 0 if ok_a else 1

    # ── B. 同一幂等键并发 ──
    env_b = seed(1)
    res_b, ms_b = timed(lambda: run_race(n, env_b["task_id"], env_b["testers"], same_key=True))
    state_b = task_state(env_b["task_id"])
    created_b, replayed_b = res_b.get("claimed", 0), res_b.get("replayed", 0)
    ok_b = (created_b == 1 and replayed_b == n - 1
            and state_b.status == "claimed" and int(state_b.version) == 1)
    print(f"\n[B] 同 1 个测试员 × **同一个**幂等键并发 {n} 次")
    print(f"    结果分布: {dict(res_b)}")
    print(f"    落库状态: status={state_b.status} version={state_b.version}")
    print(f"    耗时(整批墙钟): {ms_b:.0f} ms")
    print(f"    {'[OK]' if ok_b else '[FAIL]'} 恰好 1 次真实领取，其余全部重放（version 只 +1）")
    failed += 0 if ok_b else 1

    # ── C. 同 key 换 body ──
    env_c = seed(2)
    key = f"bench-{uuid.uuid4().hex}"
    first, second = env_c["testers"]
    first_res = claim_task(idem_key=key, task_id=env_c["task_id"], tester_id=first)
    second_res = claim_task(idem_key=key, task_id=env_c["task_id"], tester_id=second)
    state_c = task_state(env_c["task_id"])
    ok_c = (first_res.code == "claimed" and second_res.code == "body_mismatch"
            and second_res.status_code == 422
            and int(state_c.claimed_by) == first)
    print("\n[C] 同幂等键换 body（换一个测试员）")
    print(f"    首次: {first_res.code}  第二次: {second_res.code}/{second_res.status_code}")
    print(f"    落库 claimed_by={state_c.claimed_by}（要求仍是第一个测试员 {first}）")
    print(f"    {'[OK]' if ok_c else '[FAIL]'} 换 body 被 422 拒绝且无副作用")
    failed += 0 if ok_c else 1

    # ── D. ABA：领取 → 释放 → 旧版本号重领 ──
    env_d = seed(1)
    tester = env_d["testers"][0]
    task_id = env_d["task_id"]
    claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id, tester_id=tester)
    release_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id, tester_id=tester)

    stale_code = ""
    try:
        claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                   tester_id=tester, expect_version=0)
    except QueueConflict as exc:
        stale_code = exc.code

    fresh = claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                       tester_id=tester, expect_version=2)
    state_d = task_state(task_id)
    ok_d = (stale_code == "version_conflict" and fresh.code == "claimed"
            and fresh.version == 3 and int(state_d.version) == 3)
    print("\n[D] ABA 保护：领取(v1) → 释放(v2) → 旧版本号重领 → 新版本号重领")
    print(f"    旧版本号(v0)重领: {stale_code or '未拦截'}")
    print(f"    新版本号(v2)重领: {fresh.code} version={fresh.version}")
    print(f"    落库状态: status={state_d.status} version={state_d.version}")
    print(f"    {'[OK]' if ok_d else '[FAIL]'} 陈旧请求被 409 version_conflict 拦住")
    failed += 0 if ok_d else 1

    print("\n" + "=" * 74)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
