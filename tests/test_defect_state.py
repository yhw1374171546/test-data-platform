"""问题单状态机的**穷举**测试（纯函数，不需要数据库）。

期望值在这里被**重新写了一遍**（而不是从 TRANSITIONS 里读出来对比自己）：
如果用实现去验证实现，这个测试永远不会失败，也就永远没有价值。
"""
from __future__ import annotations

import pytest

from app.domain.defect_state import (STATUSES, IllegalTransition,
                                     UnknownStatus, allowed_next, can_transition,
                                     check_transition, is_known, legal_pairs,
                                     matrix, normalize)

# 手写的期望矩阵：这是"业务认可"的唯一来源，代码必须服从它
EXPECTED: dict[str, set[str]] = {
    "open":      {"triaged", "rejected"},
    "triaged":   {"assigned", "rejected"},
    "assigned":  {"fixing", "rejected"},
    "fixing":    {"verifying", "reopened"},
    "verifying": {"closed", "reopened"},
    "reopened":  {"assigned", "rejected"},
    "rejected":  {"closed", "open"},
    "closed":    {"reopened"},
}


def test_status_set_matches_expectation() -> None:
    assert set(STATUSES) == set(EXPECTED), "状态枚举和期望表不一致"
    assert len(STATUSES) == len(set(STATUSES)), "状态名必须唯一"


@pytest.mark.parametrize("src", sorted(EXPECTED))
def test_every_row_of_matrix(src: str) -> None:
    """逐行核对 8×8 矩阵：合法必须 True，非法必须 False。"""
    for dst in STATUSES:
        expected = dst in EXPECTED[src]
        assert can_transition(src, dst) is expected, (
            f"{src} -> {dst} 期望 {expected}，实际 {can_transition(src, dst)}"
        )


def test_no_self_transition() -> None:
    """自环一律不合法：重复提交由 service 层识别为 already，不算一次转移。"""
    for status in STATUSES:
        assert can_transition(status, status) is False
        assert status not in allowed_next(status)


def test_legal_pair_count() -> None:
    """15 条合法边（8 行里每行 1~2 条）。数字写死，防止有人"顺手"放宽状态机。"""
    assert len(legal_pairs()) == 15
    assert sum(len(v) for v in EXPECTED.values()) == 15


def test_matrix_is_square() -> None:
    m = matrix()
    assert len(m) == len(STATUSES)
    for row in m.values():
        assert len(row) == len(STATUSES)


def test_every_status_reachable() -> None:
    """每个状态都既可达又有出边（没有孤岛）。

    注意 `open` 也是可达的：`rejected → open`（驳回后补充信息重开），
    所以这张状态图是**强连通**的，不存在真正的"终态"。
    """
    targets = {dst for _, dst in legal_pairs()}
    sources = {src for src, _ in legal_pairs()}
    assert targets == set(STATUSES), "每个状态都应该能被到达"
    assert sources == set(STATUSES), "每个状态都应该有出边"


def test_normalize_and_known() -> None:
    assert normalize("  OPEN ") == "open"
    assert is_known("closed") is True
    assert is_known("done") is False
    with pytest.raises(UnknownStatus):
        normalize("done")


def test_check_transition_raises_on_illegal() -> None:
    check_transition("open", "triaged")               # 合法：不抛
    with pytest.raises(IllegalTransition) as exc:
        check_transition("open", "closed")            # 跳步
    assert exc.value.from_status == "open"
    assert exc.value.to_status == "closed"
    assert exc.value.allowed == ["rejected", "triaged"]

    with pytest.raises(IllegalTransition):
        check_transition("open", "open")              # 自环走 check 属于误用


def test_unknown_status_in_allowed_next_is_empty() -> None:
    """未知状态查"下一步"返回空集而不是抛异常——方便做推荐动作接口。"""
    assert allowed_next("not-a-status") == frozenset()
