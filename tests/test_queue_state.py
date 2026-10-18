"""执行队列状态规则的纯函数测试（不需要数据库）。

数量上比问题单状态机少得多，这是有意的：队列的分支本来就少，
**真正的正确性在 `WHERE status='pending' AND version=?` 那条 UPDATE 上**，
在数据库里验证（tests/test_claim_flow.py），不在这里堆断言充数。
"""
from __future__ import annotations

import pytest

from app.domain.queue_state import (CLAIMABLE, FINISHED, QUEUE_STATUSES,
                                    STATUS_LABEL, UnknownQueueStatus,
                                    can_finish, can_release, is_claimable,
                                    is_known, is_owned, normalize_status)


def test_status_sets_are_consistent() -> None:
    assert set(QUEUE_STATUSES) == {"pending", "claimed", "running", "done", "cancelled"}
    assert CLAIMABLE == {"pending"}
    assert FINISHED == {"done", "cancelled"}
    # 每个状态都要有中文标签，否则前端会漏显示
    assert set(STATUS_LABEL) == set(QUEUE_STATUSES)


def test_only_pending_is_claimable() -> None:
    assert is_claimable("pending") is True
    for status in ("claimed", "running", "done", "cancelled"):
        assert is_claimable(status) is False
    assert is_claimable("") is False
    assert is_claimable("PENDING") is True          # 大小写不敏感


def test_owned_states_can_release_and_finish() -> None:
    for status in ("claimed", "running"):
        assert is_owned(status) is True
        assert can_release(status) is True
        assert can_finish(status) is True
    for status in ("pending", "done", "cancelled"):
        assert can_release(status) is False
        assert can_finish(status) is False


def test_done_cannot_go_back_to_pending() -> None:
    """完成的任务不能释放回队列：要重跑就新建任务，否则执行结果和任务对不上。"""
    assert can_release("done") is False
    assert can_release("cancelled") is False


def test_normalize_and_known() -> None:
    assert normalize_status(" CLAIMED ") == "claimed"
    assert is_known("done") is True
    assert is_known("finished") is False
    with pytest.raises(UnknownQueueStatus):
        normalize_status("finished")
