"""占用方案 A/B/C 的正确性测试（技术方案 §4.1）。

三种方案共用同一套业务代码，只换"怎么抢"。所以这里验的是：
**不管用哪种抢法，正确性都由数据库兜底** —— 同一档期最终只能有一个有效预约。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from app.infra import cache, redis_lock
from app.infra.db import session_scope
from app.service.booking import (SCHEMES, SlotNotAvailable, create_booking)
from sqlalchemy import text


@pytest.fixture(scope="session")
def redis_ok() -> None:
    cache.reset_connection()
    if not cache.available():
        pytest.skip("Redis 不可用（先跑 dev.cmd init 拉起 Redis）")


def _book(refs: Dict[str, Any], idem, scheme: str, day: str,
          half: int = 0) -> Any:
    return create_booking(idem_key=idem(), project_id=refs["project_id"],
                          vehicle_id=refs["vehicle_id"], testers_id=refs["tester_id"],
                          slot_date=day, half=half, scheme=scheme)


def _active_bookings(refs: Dict[str, Any], day: str) -> int:
    with session_scope() as s:
        return int(s.execute(text("""
            SELECT count(*) FROM bookings
             WHERE vehicle_id = :v AND slot_date = :d AND status = 'active'
        """), {"v": refs["vehicle_id"], "d": day}).scalar_one())


def test_unknown_scheme_is_rejected(refs: Dict[str, Any], idem) -> None:
    with pytest.raises(ValueError):
        _book(refs, idem, "magic", "2035-01-01")


def test_pessimistic_scheme_books_exactly_once(refs: Dict[str, Any], idem) -> None:
    day = "2035-01-02"
    first = _book(refs, idem, "pessimistic", day)
    assert first.code == "created"

    with pytest.raises(SlotNotAvailable):
        _book(refs, idem, "pessimistic", day)
    assert _active_bookings(refs, day) == 1


def test_schemes_share_the_same_invariant(refs: Dict[str, Any], idem,
                                          redis_ok: None) -> None:
    """A 抢到之后，B/C 都抢不到 —— 三种抢法共用同一个唯一约束。"""
    day = "2035-01-03"
    assert _book(refs, idem, "conditional", day).code == "created"
    with pytest.raises(SlotNotAvailable):
        _book(refs, idem, "pessimistic", day)
    with pytest.raises(SlotNotAvailable):
        _book(refs, idem, "redis", day)
    assert _active_bookings(refs, day) == 1


def test_redis_scheme_reserves_and_books(refs: Dict[str, Any], idem,
                                         redis_ok: None) -> None:
    day = "2035-01-04"
    assert _book(refs, idem, "redis", day).code == "created"

    # 预占还在（TTL 内），第二次会在 Redis 这一层就被挡掉
    with pytest.raises(SlotNotAvailable):
        _book(refs, idem, "redis", day)
    assert _active_bookings(refs, day) == 1


def test_redis_reservation_released_on_cancel(refs: Dict[str, Any], idem,
                                              redis_ok: None) -> None:
    """取消后必须能约回来 —— 不能出现"取消了却约不回来"（预占残留到 TTL）。"""
    day = "2035-01-05"
    first = _book(refs, idem, "redis", day)
    assert first.code == "created"

    from app.service.booking import cancel_booking
    assert cancel_booking(idem_key=idem(), booking_no=first.booking_no).code == "cancelled"

    again = _book(refs, idem, "redis", day)
    assert again.code == "created", "取消后预占没释放，档期会被幽灵占位"


def test_redis_concurrent_only_one_wins(refs: Dict[str, Any], idem,
                                       redis_ok: None) -> None:
    day = "2035-01-06"
    n = 20
    outcomes: List[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            res = create_booking(idem_key=idem(), project_id=refs["project_id"],
                                 vehicle_id=refs["vehicle_id"],
                                 testers_id=refs["tester_id"], slot_date=day,
                                 half=0, scheme="redis")
            tag = res.code
        except SlotNotAvailable:
            tag = "slot_taken"
        except Exception as exc:                      # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("created") == 1, f"恰好一人成功，实际：{outcomes}"
    assert _active_bookings(refs, day) == 1


def test_all_schemes_are_listed() -> None:
    assert SCHEMES == ("conditional", "pessimistic", "redis")
    assert redis_lock.RESERVE_TTL >= 1
