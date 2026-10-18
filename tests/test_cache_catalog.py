"""缓存一致性测试：**三种失效策略 + 负对照**（技术方案 §4.3）。

这组用例最想证明的是"**实验能测出脏读**"：
如果只测 A/B/C 且都是"没脏读"，你没法确认是方案扎实还是实验没打中窗口。
所以 `test_none_strategy_serves_stale` 是这组里最重要的一条 ——
它故意不失效缓存，然后断言"读到了旧值"。这条绿了，其它几条的"没脏读"才有意义。

需要 Redis；没起 Redis 时整组跳过（`dev.cmd` 会自动拉起）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict

import pytest
from sqlalchemy import text

from app.infra import cache
from app.infra.db import session_scope
from app.service.catalog import get_case, get_vehicle, update_vehicle
from app.service.claim import enqueue_task
from app.service.outbox import (process_batch, process_cache_invalidations)


@pytest.fixture(scope="session")
def redis_ok() -> None:
    cache.reset_connection()
    if not cache.available():
        pytest.skip("Redis 不可用（先跑 dev.cmd init 拉起 Redis，或手工启动 6380）")


@pytest.fixture(autouse=True)
def clean_cache(redis_ok: None):
    cache.clear_all()
    cache.reset_stats()
    cache.set_strategy("delete")
    yield
    cache.set_strategy("delete")
    cache.reset_stats()
    cache.clear_all()


def cached_value(vin: str) -> Any:
    return cache.get_json(cache.key_vehicle(vin))


# ────────────────────────── 基本行为 ──────────────────────────
def test_read_then_hit(refs: Dict[str, Any]) -> None:
    first = get_vehicle(refs["vin"])
    second = get_vehicle(refs["vin"])

    assert first["_cache"] == "miss" and second["_cache"] == "hit"
    assert second["vin"] == refs["vin"]
    stats = cache.stats()
    assert (stats["hits"], stats["misses"]) == (1, 1)
    assert cached_value(refs["vin"])["vin"] == refs["vin"]


def test_off_strategy_bypasses_cache(refs: Dict[str, Any]) -> None:
    cache.set_strategy("off")
    data = get_vehicle(refs["vin"])
    assert data["_cache"] == "bypass"
    assert cached_value(refs["vin"]) is None, "off 策略下不应该写缓存"


def test_miss_then_update_then_miss(refs: Dict[str, Any]) -> None:
    """A 方案：提交后删缓存 → 下一次读回源拿到新值。"""
    get_vehicle(refs["vin"])                                   # 填充
    update_vehicle(vin=refs["vin"], location="临港-B区")
    data = get_vehicle(refs["vin"])
    assert data["location"] == "临港-B区" and data["_cache"] == "miss"


# ────────────────────────── 负对照（最重要的一条）──────────────────────────
def test_none_strategy_serves_stale(refs: Dict[str, Any]) -> None:
    """故意不失效缓存：写完再读，必须读到**旧值**——证明这套检测能抓到脏读。"""
    cache.set_strategy("none")
    before = get_vehicle(refs["vin"])
    assert before["_cache"] == "miss"

    update_vehicle(vin=refs["vin"], location="临港-脏读验证")
    after = get_vehicle(refs["vin"])

    assert after["_cache"] == "hit", "负对照必须命中缓存，否则测不到脏读"
    assert after["location"] == before["location"], "负对照下读到的就是旧值"
    assert after["location"] != "临港-脏读验证"


# ────────────────────────── 延迟双删 ──────────────────────────
def test_double_delete_clears_after_delay(refs: Dict[str, Any]) -> None:
    cache.set_strategy("double_delete")
    get_vehicle(refs["vin"])

    update_vehicle(vin=refs["vin"], location="临港-C区")
    # 写前删了、写后还有一次延迟删除；等延迟过去，key 应该已不存在
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and cached_value(refs["vin"]) is not None:
        time.sleep(0.02)
    assert cached_value(refs["vin"]) is None, "延迟删除没有生效"
    assert get_vehicle(refs["vin"])["location"] == "临港-C区"


# ────────────────────────── outbox 驱动 ──────────────────────────
def test_outbox_strategy_invalidated_by_worker(refs: Dict[str, Any]) -> None:
    cache.set_strategy("outbox")
    get_vehicle(refs["vin"])

    update_vehicle(vin=refs["vin"], location="临港-D区")

    # 写事务里应该留下一条失效消息；key 还没被删（消费是异步的）
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT topic, status, payload FROM outbox
             WHERE topic = :t ORDER BY id DESC LIMIT 5
        """), {"t": cache.TOPIC_CACHE_INVALIDATE}).all()
    assert rows, "outbox 策略必须写入失效消息"
    assert rows[0].payload["keys"] == [cache.key_vehicle(refs["vin"])]
    assert cached_value(refs["vin"]) is not None, "消息还没被消费，缓存不该已经消失"

    # 用**批量消费**路径模拟 worker：这条路一次事务删一批 key。
    # 换成逐条的 process_batch 会被压测留下的缓存失效积压吃掉配额（踩过），
    # 而且生产里这类消息本来就该批量消费。
    consumed = process_cache_invalidations(limit=500)
    assert consumed["messages"] >= 1

    assert cached_value(refs["vin"]) is None, "消费后 key 必须被删"
    read = get_vehicle(refs["vin"])
    assert read["location"] == "临港-D区" and read["_cache"] == "miss"


def test_outbox_strategy_message_is_replayed_if_cache_down(refs: Dict[str, Any]) -> None:
    """缓存不可用时，失效消息不能静默成功 —— 必须留在 pending 等重试。"""
    cache.set_strategy("outbox")
    get_vehicle(refs["vin"])
    update_vehicle(vin=refs["vin"], location="临港-E区")

    from app.infra import cache as cache_mod
    original_url = cache_mod.REDIS_URL
    try:
        cache_mod.REDIS_URL = "redis://127.0.0.1:6399/0"     # 指向一个没有 Redis 的端口
        cache_mod.reset_connection()
        stats = process_batch(limit=10)
        assert stats["failed"] >= 1, "缓存挂了，消息应该处理失败而不是 done"
    finally:
        cache_mod.REDIS_URL = original_url
        cache_mod.reset_connection()
        # 把状态复原，别把失败留给后面的用例
        with session_scope() as s:
            s.execute(text("""
                UPDATE outbox SET status = 'pending', attempts = 0, next_at = now()
                 WHERE topic = :t
            """), {"t": cache.TOPIC_CACHE_INVALIDATE})
        process_batch(limit=10)


# ────────────────────────── 用例详情（跨模块失效）──────────────────────────
def test_case_cache_invalidated_by_enqueue(refs: Dict[str, Any], idem) -> None:
    """导入用例时会 upsert title —— 用例详情缓存必须跟着失效。"""
    case_no = f"TC-CACHE-{uuid.uuid4().hex[:6]}"
    enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                 version_id=refs["version_id"], case_no=case_no,
                 case_title="初始标题")
    first = get_case(case_no)
    assert first["title"] == "初始标题" and first["_cache"] == "miss"
    assert get_case(case_no)["_cache"] == "hit"

    # 同一个 case_no 在**另一个版本**下再导入（标题变了）→ 缓存要被失效。
    # 用另一个版本是因为 (项目, 版本, 用例) 唯一约束会挡住同版本重复排队 ——
    # 重复排队本身是 409，那是另一条已测过的规则。
    with session_scope() as s:
        version2 = int(s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-{refs['tag']}-2"}).scalar_one())
    enqueue_task(idem_key=idem(), project_id=refs["project_id"],
                 version_id=version2, case_no=case_no, case_title="改过的标题")

    second = get_case(case_no)
    assert second["title"] == "改过的标题", "改标题后还读到旧值，说明缓存没失效"
    assert second["_cache"] == "miss"


def test_vehicle_not_found(refs: Dict[str, Any]) -> None:
    from app.service.booking import RefNotFound
    with pytest.raises(RefNotFound):
        get_vehicle("VIN-NOT-EXIST")
