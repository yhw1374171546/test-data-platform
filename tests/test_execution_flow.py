"""提交执行结果的落库测试：四表同事务、幂等、并发、业务日期口径（技术方案 §4.4 / §9）。

这组用例最想守住的三件事：
  1. **原子性**：一次提交要么 executions + outbox 都在，要么都不在（失败不留半条数据）
  2. **恰好一次**：并发提交同一条任务，只有一次落库，其余 409
  3. **日期口径**：`biz_date` 必须按本地自然日算，边界那一小时不能算错天
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import text

from app.domain.biz_time import to_biz_date
from app.infra.db import session_scope
from app.service.claim import QueueConflict, claim_task
from app.service.execution import submit_execution

UTC = timezone.utc


# ────────────────────────── 小工具 ──────────────────────────
def claimed_task(refs: Dict[str, Any], make_task, idem) -> int:
    task_id = make_task()
    claim_task(idem_key=idem(), task_id=task_id, tester_id=refs["tester_id"])
    return task_id


def task_state(task_id: int) -> Any:
    with session_scope() as s:
        return s.execute(text("""
            SELECT status, claimed_by, version FROM execution_queue WHERE id = :id
        """), {"id": task_id}).one()


def execution_rows(task_id: int) -> List[Any]:
    with session_scope() as s:
        return s.execute(text("""
            SELECT id, result, note, tester_id, vehicle_id, executed_at
              FROM executions WHERE queue_id = :q ORDER BY id
        """), {"q": task_id}).all()


def outbox_rows(task_id: int) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id, topic, status, attempts, payload
              FROM outbox WHERE payload ->> 'queue_id' = :q ORDER BY id
        """), {"q": str(task_id)}).all()
    return [{"id": int(r.id), "topic": r.topic, "status": r.status,
             "attempts": int(r.attempts), "payload": dict(r.payload)} for r in rows]


def defects_of_execution(execution_id: int) -> List[Any]:
    with session_scope() as s:
        return s.execute(text("""
            SELECT d.defect_no, d.status, d.severity, d.execution_id
              FROM defects d
              JOIN defect_links l ON l.defect_id = d.id
             WHERE l.execution_id = :e
        """), {"e": execution_id}).all()


# ────────────────────────── 正常路径 ──────────────────────────
def test_submit_pass_writes_execution_and_outbox(refs: Dict[str, Any], make_task,
                                                 idem) -> None:
    task_id = claimed_task(refs, make_task, idem)

    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result="pass",
                           note="全部通过", vehicle_id=refs["vehicle_id"])

    assert res.ok and res.code == "recorded" and res.status_code == 201
    assert res.biz_date == to_biz_date(res.executed_at)
    assert res.defect_no is None

    rows = execution_rows(task_id)
    assert len(rows) == 1
    assert rows[0].result == "pass" and rows[0].note == "全部通过"
    assert int(rows[0].vehicle_id) == refs["vehicle_id"]

    assert task_state(task_id).status == "done"
    assert task_state(task_id).version == 2, "领取 +1、完成再 +1"

    outbox = outbox_rows(task_id)
    assert len(outbox) == 1, "一次提交只能产生一条 outbox 消息"
    msg = outbox[0]
    assert msg["topic"] == "exec_result_recorded"
    assert msg["status"] == "pending" and msg["attempts"] == 0
    assert msg["payload"]["execution_id"] == res.execution_id
    assert msg["payload"]["result"] == "pass"
    assert msg["payload"]["biz_date"] == res.biz_date.isoformat()
    assert msg["payload"]["project_id"] == refs["project_id"]
    assert msg["payload"]["version_id"] == refs["version_id"]


def test_submit_fail_without_defect_is_allowed(refs: Dict[str, Any], make_task,
                                               idem) -> None:
    """失败但没提单：接口不拦，留给第 3 周的对账（fail_without_defect）去发现。"""
    task_id = claimed_task(refs, make_task, idem)

    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result="fail",
                           note="左转压线，暂未提单")

    assert res.code == "recorded" and res.defect_no is None
    with session_scope() as s:
        count = s.execute(text("SELECT count(*) FROM defects WHERE project_id = :p"),
                          {"p": refs["project_id"]}).scalar_one()
    assert int(count) == 0, "没带 defect 就不该凭空造一条缺陷"
    assert len(outbox_rows(task_id)) == 1, "但明细和 outbox 必须照常落库"


def test_submit_fail_with_defect_links_execution(refs: Dict[str, Any], make_task,
                                                 idem) -> None:
    task_id = claimed_task(refs, make_task, idem)

    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result="fail",
                           note="无保护左转压线",
                           defect={"title": "NOA 无保护左转压线", "severity": "S1"})

    assert res.code == "recorded" and res.defect_no
    linked = defects_of_execution(int(res.execution_id))
    assert len(linked) == 1
    assert linked[0].defect_no == res.defect_no
    assert int(linked[0].execution_id) == res.execution_id, "执行记录是缺陷的来源"
    assert linked[0].status == "open" and linked[0].severity == "S1"

    # 审计里要有一条"(new) → open"，actor 能追到人
    with session_scope() as s:
        audit = s.execute(text("""
            SELECT from_status, to_status, actor FROM defect_transitions
             WHERE defect_id = (SELECT id FROM defects WHERE defect_no = :no)
        """), {"no": res.defect_no}).all()
    assert [(a.from_status, a.to_status) for a in audit] == [("(new)", "open")]
    assert audit[0].actor == f"tester:{refs['tester_id']}"


# ────────────────────────── 幂等 ──────────────────────────
def test_same_key_replays_same_execution_id(refs: Dict[str, Any], make_task,
                                            idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    key = idem()

    first = submit_execution(idem_key=key, task_id=task_id,
                             tester_id=refs["tester_id"], result="pass")
    second = submit_execution(idem_key=key, task_id=task_id,
                              tester_id=refs["tester_id"], result="pass")

    assert first.code == "recorded" and second.code == "replayed"
    assert second.execution_id == first.execution_id, "重放必须返回同一个 execution_id"
    assert second.biz_date == first.biz_date
    assert len(execution_rows(task_id)) == 1, "重放不能写第二条执行记录"
    assert len(outbox_rows(task_id)) == 1, "重放不能写第二条 outbox"


def test_body_mismatch_422_without_side_effect(refs: Dict[str, Any], make_task,
                                               idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    key = idem()

    submit_execution(idem_key=key, task_id=task_id,
                     tester_id=refs["tester_id"], result="pass")
    res = submit_execution(idem_key=key, task_id=task_id,
                           tester_id=refs["tester_id"], result="fail")

    assert not res.ok and res.code == "body_mismatch" and res.status_code == 422
    assert len(execution_rows(task_id)) == 1, "换 body 不得再写执行记录"


# ────────────────────────── 状态与归属 ──────────────────────────
def test_submit_when_pending_is_not_claimed(refs: Dict[str, Any], make_task,
                                            idem) -> None:
    task_id = make_task()                       # 没领取

    with pytest.raises(QueueConflict) as exc:
        submit_execution(idem_key=idem(), task_id=task_id,
                         tester_id=refs["tester_id"], result="pass")
    assert exc.value.code == "not_claimed"
    assert task_state(task_id).status == "pending"
    assert execution_rows(task_id) == [] and outbox_rows(task_id) == []


def test_submit_by_non_owner_is_409(refs: Dict[str, Any], make_task, make_testers,
                                    idem) -> None:
    task_id = make_task()
    owner, other = make_testers(2)
    claim_task(idem_key=idem(), task_id=task_id, tester_id=owner)

    with pytest.raises(QueueConflict) as exc:
        submit_execution(idem_key=idem(), task_id=task_id,
                         tester_id=other, result="pass")
    assert exc.value.code == "not_claim_owner"
    assert task_state(task_id).status == "claimed"
    # 失败必须不留半条数据（原子性的反向验证）
    assert execution_rows(task_id) == []
    assert outbox_rows(task_id) == []


def test_submit_twice_is_already_submitted(refs: Dict[str, Any], make_task,
                                           idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    submit_execution(idem_key=idem(), task_id=task_id,
                     tester_id=refs["tester_id"], result="pass")

    with pytest.raises(QueueConflict) as exc:
        submit_execution(idem_key=idem(), task_id=task_id,
                         tester_id=refs["tester_id"], result="fail")
    assert exc.value.code == "already_submitted"
    assert len(execution_rows(task_id)) == 1, "换 key 重复提交不受理"


def test_invalid_result_is_rejected(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    with pytest.raises(ValueError):
        submit_execution(idem_key=idem(), task_id=task_id,
                         tester_id=refs["tester_id"], result="ok")
    assert task_state(task_id).status == "claimed"
    assert execution_rows(task_id) == []


def test_future_executed_at_is_rejected(refs: Dict[str, Any], make_task, idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    future = datetime.now(UTC) + timedelta(days=3)
    with pytest.raises(ValueError):
        submit_execution(idem_key=idem(), task_id=task_id,
                         tester_id=refs["tester_id"], result="pass",
                         executed_at=future)


# ────────────────────────── 并发 ──────────────────────────
def test_concurrent_submits_only_one_wins(refs: Dict[str, Any], make_task,
                                          idem) -> None:
    """10 个线程拿不同 key 同时提交同一条任务 → 恰好 1 次落库。"""
    task_id = claimed_task(refs, make_task, idem)
    n = 10

    codes: List[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            res = submit_execution(idem_key=idem(), task_id=task_id,
                                   tester_id=refs["tester_id"],
                                   result="pass" if i % 2 == 0 else "fail")
            code = res.code
        except QueueConflict as exc:
            code = exc.code
        except Exception as exc:                    # noqa: BLE001
            code = f"error:{type(exc).__name__}"
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count("recorded") == 1, f"恰好 1 次落库，实际：{codes}"
    assert codes.count("already_submitted") == n - 1, f"其余应 409，实际：{codes}"
    assert len(execution_rows(task_id)) == 1
    assert len(outbox_rows(task_id)) == 1, "outbox 也必须正好一条（否则汇总会多算）"
    assert task_state(task_id).version == 2


# ────────────────────────── 业务日期口径（§9 坑 #4） ──────────────────────────
def test_biz_date_follows_local_day_boundary(refs: Dict[str, Any], make_task,
                                             idem) -> None:
    """UTC 16:30 已经是北京时间次日 —— 补录时间必须按本地日归集，否则日报每天差几条。"""
    task_id_a = claimed_task(refs, make_task, idem)
    task_id_b = claimed_task(refs, make_task, idem)

    before = datetime(2025, 1, 1, 15, 30, tzinfo=UTC)     # 北京时间 01-01 23:30
    after = datetime(2025, 1, 1, 16, 30, tzinfo=UTC)      # 北京时间 01-02 00:30

    res_a = submit_execution(idem_key=idem(), task_id=task_id_a,
                             tester_id=refs["tester_id"], result="pass",
                             executed_at=before)
    res_b = submit_execution(idem_key=idem(), task_id=task_id_b,
                             tester_id=refs["tester_id"], result="pass",
                             executed_at=after)

    assert str(res_a.biz_date) == "2025-01-01"
    assert str(res_b.biz_date) == "2025-01-02"

    payloads = {row["payload"]["queue_id"]: row["payload"]
                for row in outbox_rows(task_id_a) + outbox_rows(task_id_b)}
    assert payloads[task_id_a]["biz_date"] == "2025-01-01"
    assert payloads[task_id_b]["biz_date"] == "2025-01-02"

    # 落库的 executed_at 不能因为 biz_date 换算而偏移
    stored = [r.executed_at for r in execution_rows(task_id_a) + execution_rows(task_id_b)]
    assert all(dt.tzinfo is not None for dt in stored)
    assert {dt.astimezone(UTC).isoformat() for dt in stored} == {
        before.isoformat(), after.isoformat()}


def test_naive_executed_at_is_treated_as_utc(refs: Dict[str, Any], make_task,
                                             idem) -> None:
    task_id = claimed_task(refs, make_task, idem)
    naive = datetime(2025, 6, 1, 16, 30)                  # 漏了时区
    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result="pass",
                           executed_at=naive)
    assert str(res.biz_date) == "2025-06-02", "naive 按 UTC 解释 → 北京次日"
