"""主数据详情：**带缓存的读 + 会失效的写**（技术方案 §4.3 的落点）。

缓存的是"车辆详情"和"用例详情"——它们满足缓存的三个前提：
  ① 读远多于写（测试员反复看同一台车/同一条用例）；
  ② 单条数据小；
  ③ **能容忍一小段时间的旧值**（看板/下拉框，不是扣库存那种强一致场景）。

失效策略的实现在 `app/infra/cache.py`；这里只负责把"写路径"和"失效动作"绑在一起，
并且保证一个顺序：**先提交事务，再删缓存**（`double_delete` 是写前也删一次）。

⚠️ 一个反直觉但重要的点：删缓存**必须**在事务提交之后。
如果在事务里删、之后事务回滚了，缓存被删掉而库里还是旧值 —— 那只是白删一次（可接受）；
但如果先删、再提交失败，会出现"缓存没有、库里旧"的短暂不一致（也只是回源，可接受）。
真正危险的是反过来：先删、别的事务/读取把**旧值**回填进缓存（这才是要防的窗口）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.infra import cache, metrics
from app.infra.db import session_scope
from app.service.booking import RefNotFound

VEHICLE_STATUSES = ("available", "in_test", "maintenance", "retired")


# ────────────────────────── 车辆 ──────────────────────────
def get_vehicle(vin: str) -> Dict[str, Any]:
    """车辆详情。返回里带 `_cache` 字段（hit / miss / bypass），方便接入层回 `X-Cache`。"""
    key = cache.key_vehicle(vin)

    if cache.caching_enabled():
        cached = cache.get_json(key, entity="vehicle")
        if cached is not None:
            return {**cached, "_cache": "hit"}

    data = _load_vehicle(vin)
    if cache.caching_enabled() and cache.set_json(key, data):
        return {**data, "_cache": "miss"}
    return {**data, "_cache": "bypass"}


def update_vehicle(*, vin: str, status: Optional[str] = None,
                   location: Optional[str] = None,
                   plate_no: Optional[str] = None) -> Dict[str, Any]:
    """更新车辆并失效缓存。三个字段都可选，至少要给一个。"""
    if status is not None and status not in VEHICLE_STATUSES:
        raise ValueError(f"status 只能是 {'/'.join(VEHICLE_STATUSES)}，收到 {status!r}")
    if status is None and location is None and plate_no is None:
        raise ValueError("至少要更新一个字段（status / location / plate_no）")

    key = cache.key_vehicle(vin)
    strat = cache.strategy()

    # B 方案：写前先删一次（把"写之前就已经在缓存里的旧值"清掉）
    if strat == "double_delete":
        cache.delete(key)

    sets, params = [], {"vin": vin}
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if location is not None:
        sets.append("location = :location")
        params["location"] = location
    if plate_no is not None:
        sets.append("plate_no = :plate_no")
        params["plate_no"] = plate_no

    with session_scope() as s:
        row = s.execute(text(f"""
            UPDATE vehicles SET {', '.join(sets)} WHERE vin = :vin
            RETURNING id, vin, model, plate_no, status, location
        """), params).first()
        if row is None:
            raise RefNotFound("vehicle", vin)

        # C 方案：把"失效"写成消息，和业务写入同一个事务 —— 删除不会因为进程崩掉而丢失
        enqueue_invalidation(s, [key], reason="vehicle_updated")

    data = dict(row._mapping)

    # A 方案 / B 方案的第二次删除：事务提交之后才删
    after_commit_invalidate(key, reason="vehicle_updated")
    return data


# ────────────────────────── 用例 ──────────────────────────
def get_case(case_no: str) -> Dict[str, Any]:
    key = cache.key_case(case_no)

    if cache.caching_enabled():
        cached = cache.get_json(key, entity="case")
        if cached is not None:
            return {**cached, "_cache": "hit"}

    data = _load_case(case_no)
    if cache.caching_enabled() and cache.set_json(key, data):
        return {**data, "_cache": "miss"}
    return {**data, "_cache": "bypass"}


# ────────────────────────── 失效原语 ──────────────────────────
def enqueue_invalidation(session: Any, keys: List[str], *, reason: str) -> None:
    """C 方案：在**调用方事务**里写一条失效消息（worker 消费后才真正删 key）。

    只在 `outbox` 策略下写；其它策略什么都不做（A/B 走 `after_commit_invalidate`）。
    """
    if cache.strategy() != "outbox" or not keys:
        return
    import json

    session.execute(text("""
        INSERT INTO outbox (topic, payload) VALUES (:topic, CAST(:payload AS JSONB))
    """), {"topic": cache.TOPIC_CACHE_INVALIDATE,
           "payload": json.dumps({"keys": list(keys), "reason": reason},
                                 ensure_ascii=False)})


def after_commit_invalidate(*keys: str, reason: str) -> None:
    """A/B 方案：事务提交后删 key（B 额外安排一次延迟删除）。"""
    strat = cache.strategy()
    if strat == "delete":
        cache.delete(*keys)
        metrics.CACHE_INVALIDATIONS.labels(reason=reason).inc()
    elif strat == "double_delete":
        for key in keys:
            cache.schedule_delayed_delete(key)
        metrics.CACHE_INVALIDATIONS.labels(reason=reason).inc()
    # none = 负对照（故意不删）；off / outbox = 不走这条路


# ────────────────────────── 回源 ──────────────────────────
def _load_vehicle(vin: str) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT id, vin, model, plate_no, status, location
              FROM vehicles WHERE vin = :vin
        """), {"vin": vin}).first()
    if row is None:
        raise RefNotFound("vehicle", vin)
    return dict(row._mapping)


def _load_case(case_no: str) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT id, case_no, module, title, priority, precondition, steps_summary
              FROM test_cases WHERE case_no = :no
        """), {"no": case_no}).first()
    if row is None:
        raise RefNotFound("test_case", case_no)
    return dict(row._mapping)
