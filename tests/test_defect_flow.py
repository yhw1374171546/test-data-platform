"""问题单状态机**落库**测试：合法性、审计、幂等、并发（技术方案 §4.5 的验收）。

对应验收标准：
    "遍历所有 (from,to) 组合 → 合法全过、非法全 409、**审计条数 == 合法转移数**"

所以这里真的把 64 个组合跑了一遍，而且每条组合都用**独立的缺陷单**，
避免"上一轮把状态改坏了导致下一轮结论失真"。
"""
from __future__ import annotations

import threading
from itertools import product
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import text

from app.domain.defect_state import (STATUSES, IllegalTransition, UnknownStatus,
                                     can_transition)
from app.infra.db import session_scope
from app.service.defect import (BadCursor, DefectNotFound, create_defect,
                                get_defect, list_defects, transition_defect)

# ────────────────────────── 小工具 ──────────────────────────
def new_defect(refs: Dict[str, Any], key: str, title: str = "NOA 左转压线") -> str:
    res = create_defect(idem_key=key, title=title, severity="S2",
                        project_id=refs["project_id"], actor="qa:单测")
    assert res.ok and res.code == "created", res
    return res.defect_no


def audit_rows(defect_no: str) -> List[Tuple[str, str]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT t.from_status, t.to_status
              FROM defect_transitions t
              JOIN defects d ON d.id = t.defect_id
             WHERE d.defect_no = :no
             ORDER BY t.id
        """), {"no": defect_no}).all()
    return [(r[0], r[1]) for r in rows]


def status_of(defect_no: str) -> str:
    with session_scope() as s:
        return s.execute(text("SELECT status FROM defects WHERE defect_no = :no"),
                         {"no": defect_no}).scalar_one()


def force_status(defect_no: str, status: str) -> None:
    """绕过状态机直接改状态：为了给矩阵测试准备"从任意状态出发"的起点。

    只允许测试这么干——生产代码唯一的写法是 transition_defect。
    """
    with session_scope() as s:
        s.execute(text("UPDATE defects SET status = :s WHERE defect_no = :no"),
                  {"s": status, "no": defect_no})


# ────────────────────────── 穷举矩阵 ──────────────────────────
def test_full_matrix_legal_illegal_and_audit(refs: Dict[str, Any], idem) -> None:
    legal_count = 0
    audit_delta = 0

    for frm, to in product(STATUSES, STATUSES):
        no = new_defect(refs, idem())
        force_status(no, frm)
        before = audit_rows(no)

        if frm == to:
            # 自环 = 幂等重放，不是转移：200 + already，不写审计
            res = transition_defect(idem_key=idem(), defect_no=no,
                                    to_status=to, actor="qa:matrix")
            assert res.ok and res.code == "already" and res.already
            assert status_of(no) == frm

        elif can_transition(frm, to):
            res = transition_defect(idem_key=idem(), defect_no=no,
                                    to_status=to, actor="qa:matrix",
                                    reason=f"{frm}->{to}")
            assert res.ok and res.code == "moved", (frm, to, res)
            assert res.from_status == frm and res.to_status == to
            assert status_of(no) == to
            legal_count += 1

        else:
            # 非法转移必须抛异常（→ 409），且状态和审计都不能变
            with pytest.raises(IllegalTransition) as exc:
                transition_defect(idem_key=idem(), defect_no=no,
                                  to_status=to, actor="qa:matrix")
            assert exc.value.from_status == frm and exc.value.to_status == to
            assert status_of(no) == frm, f"非法转移把 {frm} 改成了别的状态"

        after = audit_rows(no)
        audit_delta += len(after) - len(before)
        if frm != to and not can_transition(frm, to):
            assert after == before, "非法转移不能写审计"

    assert legal_count == 15, f"合法转移应有 15 条，实际 {legal_count}"
    assert audit_delta == legal_count, (
        f"审计条数必须等于合法转移数：审计新增 {audit_delta}，合法转移 {legal_count}")


# ────────────────────────── 幂等 ──────────────────────────
def test_same_key_replays_first_response(refs: Dict[str, Any], idem) -> None:
    no = new_defect(refs, idem())
    key = idem()

    first = transition_defect(idem_key=key, defect_no=no, to_status="triaged",
                              actor="qa:a", reason="分诊")
    second = transition_defect(idem_key=key, defect_no=no, to_status="triaged",
                               actor="qa:a", reason="分诊")

    assert first.code == "moved"
    assert second.code == "replayed" and second.status_code == 200
    # 重放"原样返回首次响应"，所以 from 是首次的 open 而不是当前状态 triaged
    assert second.from_status == "open" and second.to_status == "triaged"
    assert len(audit_rows(no)) == 2, "重放不能再写一条审计"


def test_different_key_same_target_is_already(refs: Dict[str, Any], idem) -> None:
    no = new_defect(refs, idem())
    transition_defect(idem_key=idem(), defect_no=no, to_status="triaged", actor="qa:a")
    before = audit_rows(no)

    res = transition_defect(idem_key=idem(), defect_no=no, to_status="triaged",
                            actor="qa:b")
    assert res.ok and res.code == "already" and res.already
    assert audit_rows(no) == before, "重复提交同一目标状态不能写审计"


def test_body_mismatch_422_without_side_effect(refs: Dict[str, Any], idem) -> None:
    no = new_defect(refs, idem())
    key = idem()
    transition_defect(idem_key=key, defect_no=no, to_status="triaged", actor="qa:a")
    before = audit_rows(no)

    res = transition_defect(idem_key=key, defect_no=no, to_status="assigned",
                            actor="qa:a")
    assert not res.ok and res.code == "body_mismatch" and res.status_code == 422
    assert status_of(no) == "triaged", "同 key 换 body 不得产生任何状态变更"
    assert audit_rows(no) == before


def test_illegal_transition_does_not_burn_idempotency_key(refs: Dict[str, Any], idem) -> None:
    """非法转移让事务整体回滚，幂等键一起回滚 —— 否则改对参数后重试会被 422 挡住。"""
    no = new_defect(refs, idem())
    key = idem()

    with pytest.raises(IllegalTransition):
        transition_defect(idem_key=key, defect_no=no, to_status="closed", actor="qa:a")

    res = transition_defect(idem_key=key, defect_no=no, to_status="triaged", actor="qa:a")
    assert res.ok and res.code == "moved", "非法的那次不应留下幂等键"


# ────────────────────────── 并发 ──────────────────────────
def test_concurrent_same_target_writes_one_audit(refs: Dict[str, Any], idem) -> None:
    """20 个测试员同时把同一张单推到 triaged：恰好 1 次真实转移、1 条审计。"""
    no = new_defect(refs, idem())
    force_status(no, "open")
    before = audit_rows(no)

    n = 20
    codes: List[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            res = transition_defect(idem_key=idem(), defect_no=no,
                                    to_status="triaged", actor=f"qa:{i}")
            tag = res.code
        except Exception as exc:                        # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        with lock:
            codes.append(tag)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count("moved") == 1, f"恰好 1 次真实转移，实际分布：{codes}"
    assert codes.count("already") == n - 1, f"其余应为 already，实际分布：{codes}"
    assert status_of(no) == "triaged"
    assert len(audit_rows(no)) == len(before) + 1, "并发下审计只能多 1 条"


# ────────────────────────── 参数与查询 ──────────────────────────
def test_unknown_status_is_400_not_409(refs: Dict[str, Any], idem) -> None:
    no = new_defect(refs, idem())
    with pytest.raises(UnknownStatus):
        transition_defect(idem_key=idem(), defect_no=no, to_status="done")


def test_defect_not_found(refs: Dict[str, Any], idem) -> None:
    with pytest.raises(DefectNotFound):
        transition_defect(idem_key=idem(), defect_no="DF-NOT-EXIST", to_status="triaged")

    with session_scope() as s:
        found = s.execute(text(
            "SELECT count(*) FROM defects WHERE defect_no = 'DF-NOT-EXIST'")).scalar_one()
    assert int(found) == 0, "404 不该在库里留下痕迹"


def test_detail_contains_audit_trail(refs: Dict[str, Any], idem) -> None:
    no = new_defect(refs, idem(), title="记忆泊车跨层失败")
    transition_defect(idem_key=idem(), defect_no=no, to_status="triaged", actor="qa:a")
    transition_defect(idem_key=idem(), defect_no=no, to_status="assigned", actor="qa:a")

    detail = get_defect(no)
    assert detail["status"] == "assigned"
    assert [t["to_status"] for t in detail["transitions"]] == ["open", "triaged", "assigned"]
    assert detail["transitions"][0]["from_status"] == "(new)"
    assert detail["allowed_next"] == ["fixing", "rejected"]


def test_keyset_pagination_no_dup_no_miss(refs: Dict[str, Any], idem) -> None:
    for i in range(5):
        new_defect(refs, idem(), title=f"分页用例 {i}")

    seen: List[str] = []
    cursor = None
    pages = 0
    while True:
        page = list_defects(project_id=refs["project_id"], limit=2, cursor=cursor)
        pages += 1
        seen.extend(d["defect_no"] for d in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert pages < 10, "分页没有正常结束"

    assert len(seen) == 5 and len(set(seen)) == 5, f"keyset 分页丢数据或重复：{seen}"


def test_bad_cursor_is_client_error(refs: Dict[str, Any]) -> None:
    with pytest.raises(BadCursor):
        list_defects(project_id=refs["project_id"], cursor="not-a-cursor")
