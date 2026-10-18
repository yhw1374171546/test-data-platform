"""任务领取落库测试：抢占、幂等、ABA、并发（技术方案 §4.2 的验收）。

对应验收标准：
    "20 个测试员并发领同一任务 → 恰好 1 人；同 key 重放返回同一个结果；换 body 同 key → 422"

技术方案原文把"20 个测试员 + 同 Idempotency-Key"写在同一句里，实际是两种场景，
这里**拆成两个用例**分别验，因为它们考的是两套不同的机制：

    · 20 个**不同**测试员 + 不同 key  → 考 `WHERE status='pending'` 的原子性（恰好一人）
    · 同 1 个测试员 + 同 key 并发重试 → 考幂等键（恰好一次真实领取，其余重放）

如果按字面把"不同测试员 + 同 key"混在一起，第二个请求会因为 body_hash 不同被判 422
——那测到的是幂等键的 body 校验，而不是抢占。这个区分写在这里，避免以后照文档抄错。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest
from sqlalchemy import text

from app.infra.db import session_scope
from app.service.booking import RefNotFound
from app.service.claim import (QueueConflict, QueueTaskNotFound, claim_task,
                               enqueue_task, list_queue, release_task)


# ────────────────────────── 小工具 ──────────────────────────
def task_row(task_id: int) -> Any:
    with session_scope() as s:
        return s.execute(text("""
            SELECT status, claimed_by, claimed_at, version
              FROM execution_queue WHERE id = :id
        """), {"id": task_id}).one()


def set_status(task_id: int, status: str) -> None:
    """绕过状态规则直接改状态：为了构造"已完成但想释放"这类边界。"""
    with session_scope() as s:
        s.execute(text("UPDATE execution_queue SET status = :s WHERE id = :id"),
                  {"s": status, "id": task_id})


# ────────────────────────── 基本领取 ──────────────────────────
def test_claim_success(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = make_task()

    res = claim_task(idem_key=idem(), task_id=task_id, tester_id=refs["tester_id"])

    assert res.ok and res.code == "claimed"
    assert res.status == "claimed" and res.version == 1
    assert res.claimed_at is not None

    row = task_row(task_id)
    assert row.status == "claimed"
    assert int(row.claimed_by) == refs["tester_id"]
    assert int(row.version) == 1


def test_claim_twice_is_already_claimed(refs: Dict[str, Any], make_task, make_testers,
                                        idem) -> None:
    task_id = make_task()
    first, second_tester = make_testers(2)

    claim_task(idem_key=idem(), task_id=task_id, tester_id=first)

    with pytest.raises(QueueConflict) as exc:
        claim_task(idem_key=idem(), task_id=task_id, tester_id=second_tester)

    assert exc.value.code == "already_claimed"
    assert exc.value.detail["claimed_by"] == first
    assert task_row(task_id).status == "claimed", "第二次失败不能改状态"


def test_claim_missing_task_is_404(refs: Dict[str, Any], idem) -> None:
    with pytest.raises(QueueTaskNotFound):
        claim_task(idem_key=idem(), task_id=999_999_999, tester_id=refs["tester_id"])


def test_claim_unknown_tester_is_404(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = make_task()
    with pytest.raises(RefNotFound):
        claim_task(idem_key=idem(), task_id=task_id, tester_id=999_999_999)
    assert task_row(task_id).status == "pending", "外键失败不能把任务领走"


# ────────────────────────── 幂等 ──────────────────────────
def test_same_key_replays_same_result(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = make_task()
    key = idem()

    first = claim_task(idem_key=key, task_id=task_id, tester_id=refs["tester_id"])
    second = claim_task(idem_key=key, task_id=task_id, tester_id=refs["tester_id"])

    assert first.code == "claimed" and second.code == "replayed"
    assert second.task_id == first.task_id
    assert second.version == first.version, "重放必须返回同一个版本号（同一次领取）"
    assert second.claimed_at == first.claimed_at, "重放必须返回同一个领取时间"
    assert task_row(task_id).version == 1, "重放不能再次 version+1"


def test_body_mismatch_is_422_without_side_effect(refs: Dict[str, Any], make_task,
                                                  make_testers, idem) -> None:
    task_id = make_task()
    first, second_tester = make_testers(2)
    key = idem()

    claim_task(idem_key=key, task_id=task_id, tester_id=first)
    res = claim_task(idem_key=key, task_id=task_id, tester_id=second_tester)

    assert not res.ok and res.code == "body_mismatch" and res.status_code == 422
    assert int(task_row(task_id).claimed_by) == first, "同 key 换 body 不得产生副作用"


# ────────────────────────── 释放与 ABA ──────────────────────────
def test_release_then_reclaim_bumps_version(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = make_task()
    tester = refs["tester_id"]

    claim_task(idem_key=idem(), task_id=task_id, tester_id=tester)
    released = release_task(idem_key=idem(), task_id=task_id, tester_id=tester)
    assert released.ok and released.code == "released" and released.version == 2
    assert task_row(task_id).status == "pending"
    assert task_row(task_id).claimed_by is None

    again = claim_task(idem_key=idem(), task_id=task_id, tester_id=tester)
    assert again.code == "claimed" and again.version == 3


def test_stale_expect_version_is_version_conflict(refs: Dict[str, Any], make_task,
                                                  idem) -> None:
    """ABA 保护：任务已被"领取又释放"，旧版本号的请求必须被拒，而不是假装领到。"""
    task_id = make_task()
    tester = refs["tester_id"]

    claim_task(idem_key=idem(), task_id=task_id, tester_id=tester)          # v1
    release_task(idem_key=idem(), task_id=task_id, tester_id=tester)        # v2, pending

    with pytest.raises(QueueConflict) as exc:
        claim_task(idem_key=idem(), task_id=task_id, tester_id=tester,
                   expect_version=0)      # 客户端还拿着 v0
    assert exc.value.code == "version_conflict"
    assert exc.value.detail["actual_version"] == 2
    assert task_row(task_id).status == "pending", "失败时不能把任务改成 claimed"

    fresh = claim_task(idem_key=idem(), task_id=task_id, tester_id=tester,
                       expect_version=2)
    assert fresh.code == "claimed" and fresh.version == 3


def test_release_by_non_owner_is_409(refs: Dict[str, Any], make_task, make_testers,
                                    idem) -> None:
    task_id = make_task()
    owner, other = make_testers(2)

    claim_task(idem_key=idem(), task_id=task_id, tester_id=owner)
    with pytest.raises(QueueConflict) as exc:
        release_task(idem_key=idem(), task_id=task_id, tester_id=other)

    assert exc.value.code == "not_claim_owner"
    assert int(task_row(task_id).claimed_by) == owner


def test_release_done_task_is_not_releasable(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = make_task()
    tester = refs["tester_id"]

    claim_task(idem_key=idem(), task_id=task_id, tester_id=tester)
    set_status(task_id, "done")

    with pytest.raises(QueueConflict) as exc:
        release_task(idem_key=idem(), task_id=task_id, tester_id=tester)
    assert exc.value.code == "not_releasable"
    assert task_row(task_id).status == "done", "已完成的任务不能被释放回队列"


# ────────────────────────── 并发（核心验收） ──────────────────────────
def test_concurrent_different_keys_exactly_one_winner(refs: Dict[str, Any], make_task,
                                                      make_testers, idem) -> None:
    """20 个测试员、20 个不同幂等键、同一条任务 → 恰好 1 人领到，其余 19 人 409。"""
    task_id = make_task()
    testers = make_testers(20)

    outcomes: List[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(testers))

    def worker(tester_id: int) -> None:
        try:
            barrier.wait(timeout=30)
            res = claim_task(idem_key=idem(), task_id=task_id, tester_id=tester_id)
            tag = res.code
        except QueueConflict as exc:
            tag = exc.code
        except Exception as exc:                       # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker, args=(t,)) for t in testers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("claimed") == 1, f"恰好 1 人领到，实际分布：{outcomes}"
    assert outcomes.count("already_claimed") == len(testers) - 1, f"其余应 409，实际：{outcomes}"

    row = task_row(task_id)
    assert row.status == "claimed"
    assert int(row.version) == 1, "只有一次真实领取，version 只能 +1"


def test_concurrent_same_key_only_one_real_claim(refs: Dict[str, Any], make_task,
                                                 idem) -> None:
    """同一个人用同一个幂等键并发重试 20 次 → 1 次真实领取 + 19 次重放。"""
    task_id = make_task()
    tester = refs["tester_id"]
    key = idem()
    n = 20

    outcomes: List[str] = []
    versions: List[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            res = claim_task(idem_key=key, task_id=task_id, tester_id=tester)
            tag = res.code
            version = res.version
        except Exception as exc:                       # noqa: BLE001
            tag, version = f"error:{type(exc).__name__}", -1
        with lock:
            outcomes.append(tag)
            versions.append(version)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("claimed") == 1, f"恰好 1 次真实领取，实际：{outcomes}"
    assert outcomes.count("replayed") == n - 1, f"其余应重放，实际：{outcomes}"
    assert len(set(versions)) == 1, f"所有重放必须返回同一个版本号，实际：{set(versions)}"
    assert int(task_row(task_id).version) == 1


# ────────────────────────── 列表 ──────────────────────────
def test_list_queue_keyset_and_filters(refs: Dict[str, Any], make_task, idem) -> None:
    tasks = [make_task(title=f"队列用例 {i}") for i in range(3)]
    claim_task(idem_key=idem(), task_id=tasks[0], tester_id=refs["tester_id"])

    page1 = list_queue(project_id=refs["project_id"], limit=2)
    assert len(page1["items"]) == 2 and page1["has_more"] is True
    assert page1["next_cursor"] == str(page1["items"][-1]["task_id"])

    page2 = list_queue(project_id=refs["project_id"], limit=2,
                       after_id=int(page1["next_cursor"]))
    seen = [t["task_id"] for t in page1["items"]] + [t["task_id"] for t in page2["items"]]
    assert sorted(seen) == sorted(tasks), "keyset 翻页不能丢数据或重复"

    pending = list_queue(project_id=refs["project_id"], status="pending")
    assert [t["task_id"] for t in pending["items"]] == tasks[1:]

    claimed = list_queue(project_id=refs["project_id"], status="claimed")
    assert [t["task_id"] for t in claimed["items"]] == [tasks[0]]
    assert claimed["items"][0]["claimed_by"] == refs["tester_id"]
    assert claimed["items"][0]["case_no"].startswith("TC-")


# ────────────────────────── 排队（导入用例） ──────────────────────────
def test_enqueue_creates_case_and_task(refs: Dict[str, Any], idem) -> None:
    res = enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                       version_id=refs["version_id"],
                       case_no="TC-NOA-9001", case_title="隧道内 NOA 退出",
                       module="领航", priority="P1")

    assert res.ok and res.code == "enqueued" and res.status_code == 201
    assert res.status == "pending"
    with session_scope() as s:
        task = s.execute(text("""
            SELECT q.status, q.version, q.project_id, q.version_id,
                   c.case_no, c.title, c.priority
              FROM execution_queue q JOIN test_cases c ON c.id = q.case_id
             WHERE q.id = :id
        """), {"id": res.task_id}).one()
    assert task.status == "pending" and int(task.version) == 0
    assert task.case_no == "TC-NOA-9001" and task.priority == "P1"
    assert int(task.project_id) == refs["project_id"]


def test_enqueue_same_task_twice_is_409(refs: Dict[str, Any], idem) -> None:
    first = enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                         version_id=refs["version_id"],
                         case_no="TC-NOA-9002", case_title="重复排队")

    with pytest.raises(QueueConflict) as exc:
        enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                     version_id=refs["version_id"],
                     case_no="TC-NOA-9002", case_title="重复排队")

    assert exc.value.code == "task_exists"
    assert exc.value.detail["task_id"] == first.task_id, "409 要把已有任务号带回去，方便直接用"


def test_enqueue_replays_same_key(refs: Dict[str, Any], idem) -> None:
    key = idem()
    first = enqueue_task(idem_key=key, project_id=refs["project_id"],
                         version_id=refs["version_id"],
                         case_no="TC-NOA-9003", case_title="幂等排队")
    second = enqueue_task(idem_key=key, project_id=refs["project_id"],
                          version_id=refs["version_id"],
                          case_no="TC-NOA-9003", case_title="幂等排队")

    assert first.code == "enqueued" and second.code == "replayed"
    assert second.task_id == first.task_id and second.case_id == first.case_id


def test_enqueue_rejects_model_mismatch(refs: Dict[str, Any], idem) -> None:
    """项目是 U8，版本却是别的车型 → 排进去也没有意义，必须拒绝。"""
    with session_scope() as s:
        other_version = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('X9', :v) RETURNING id
        """), {"v": f"V-{refs['tag']}-x9"}).scalar_one()

    with pytest.raises(QueueConflict) as exc:
        enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                     version_id=int(other_version),
                     case_no="TC-NOA-9004", case_title="车型不匹配")
    assert exc.value.code == "model_mismatch"
    assert exc.value.detail["version_model"] == "X9"


def test_enqueue_unknown_version_is_404(refs: Dict[str, Any], idem) -> None:
    with pytest.raises(RefNotFound):
        enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                     version_id=999_999_999,
                     case_no="TC-NOA-9005", case_title="版本不存在")
