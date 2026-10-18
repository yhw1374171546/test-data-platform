"""预约车辆档期：**原子占用 + 物理幂等**。

这里的每个设计决策都是面试必问的，理由写在代码里而不是文档里：

1. **占用用一条条件 UPDATE，不用 SELECT-then-UPDATE**
   `UPDATE ... WHERE status='free'` 靠 affected rows 判定是否抢到。
   先查再改在并发下会出现"两个事务都查到 free、都去改"——这就是超占。

2. **幂等键与占用在同一事务**
   如果先提交幂等键、再单独占档期，中间失败就会出现"幂等键说成功了、档期却没占"；
   反之先占档期再写幂等键，重试就会重复占。两者必须原子（见 app/infra/idempotency.py）。

3. **档期行按需物化（lazy materialization）**
   真实系统会用定时任务把未来 N 天的档期铺好；这里改成"用到时才插一行 free"，
   这样 `dev.ps1 init` 之后不跑任何 job 也能直接预约。
   并发下也安全：`ON CONFLICT (vehicle_id, slot_date, half) DO NOTHING` 保证只有一个 free 行，
   之后的竞争仍然靠第 1 条的条件 UPDATE 判定。

4. **取消 = 改预约状态 + 释放档期，同一事务，且带 booking_id 守卫**
   释放语句必须写 `WHERE ... AND booking_id = :id`：万一有历史脏数据
   （档期指向另一张预约单），也不能把别人的档期放掉。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.domain.slot_policy import describe_slot, parse_day
from app.infra.db import session_scope
from app.infra import redis_lock
from app.infra.idempotency import body_hash_of, claim, store

CREATE_SCOPE = "create_booking"
CANCEL_SCOPE = "cancel_booking"


class SlotNotAvailable(Exception):
    """档期已被占用 / 维护中。抛出后整个事务回滚（幂等键一并回滚，允许重试）。

    注意：这里**不用返回值**表达失败，是因为它必须让事务回滚。
    如果只是 return，session_scope 会照常 commit，幂等键就留下了"失败也算成功"的坑。
    """


class RefNotFound(Exception):
    """project / vehicle / tester / execution 等外键目标不存在 → 404。"""

    def __init__(self, kind: str, ident: Any):
        self.kind = kind
        self.ident = ident
        super().__init__(f"{kind} 不存在：{ident!r}")


class BookingNotFound(Exception):
    def __init__(self, booking_no: str):
        self.booking_no = booking_no
        super().__init__(f"预约单不存在：{booking_no!r}")


class BookingNotCancellable(Exception):
    """已完成的预约不能取消（active → cancelled 才合法）。"""

    def __init__(self, booking_no: str, status: str):
        self.booking_no = booking_no
        self.status = status
        super().__init__(f"预约单 {booking_no} 当前状态 {status}，不可取消")


@dataclass
class BookingResult:
    ok: bool
    code: str                       # created | cancelled | already | replayed
    booking_id: Optional[int] = None
    booking_no: str = ""
    status_code: int = 200
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def replay(self) -> bool:
        return self.code == "replayed"


# ────────────────────────── 创建 ──────────────────────────
# 三种"怎么抢"的方案（技术方案 §4.1）。**业务逻辑完全相同，只换抢占手段**，
# 这样对比出来的 P99 才是"方案本身"的差异，而不是"代码写得不一样"的差异。
SCHEMES = ("conditional", "pessimistic", "redis")
DEFAULT_SCHEME = "conditional"


def create_booking(*, idem_key: str, project_id: int, vehicle_id: int, testers_id: int,
                   slot_date: str | date_cls, half: int,
                   scheme: str = DEFAULT_SCHEME) -> BookingResult:
    """预约一个 (车辆, 日期, 半天) 档期。

    `scheme` 决定"怎么抢"（默认 A 条件更新）：

    | scheme | 抢占方式 | 适用 |
    | --- | --- | --- |
    | `conditional` | 一条 `UPDATE ... WHERE status='free'`（affected rows 判定） | 默认，单档期 |
    | `pessimistic` | 事务内 `SELECT ... FOR UPDATE` 再判断 | "先查后改"的复杂逻辑（连续占多半天） |
    | `redis` | 先 SETNX 预占，抢到的才去写库 | 抢购式峰值，挡掉 99% 的无效 DB 流量 |

    三者**最终都由数据库的唯一约束 + 条件更新兜底**，Redis 那层只是漏斗，不是真相。
    """
    if not idem_key:
        raise ValueError("create_booking 必须带幂等键")
    if scheme not in SCHEMES:
        raise ValueError(f"未知占用方案：{scheme!r}；可用：{', '.join(SCHEMES)}")

    day = parse_day(slot_date)
    body_hash = body_hash_of(project_id=project_id, vehicle_id=vehicle_id,
                             testers_id=testers_id, slot_date=str(day), half=half)

    # ── C 方案：先问 Redis，抢不到就直接失败，一次 DB 都不打 ──
    holder = uuid.uuid4().hex
    reserved = None
    if scheme == "redis":
        reserved = redis_lock.reserve(vehicle_id=vehicle_id, slot_date=str(day),
                                      half=half, holder=holder)
        if reserved is False:
            raise SlotNotAvailable(describe_slot(vehicle_id, day, half))

    try:
        result = _create_booking_tx(idem_key=idem_key, project_id=project_id,
                                    vehicle_id=vehicle_id, testers_id=testers_id,
                                    day=day, half=half, body_hash=body_hash,
                                    scheme=scheme, holder=holder)
    except Exception:
        # 预占成功但后续失败（DB 说不 free / 事务回滚）→ 把预占放掉，否则档期被"幽灵占位"
        if reserved:
            redis_lock.release(vehicle_id=vehicle_id, slot_date=str(day),
                               half=half, holder=holder)
        raise
    return result


def _create_booking_tx(*, idem_key: str, project_id: int, vehicle_id: int,
                       testers_id: int, day: date_cls, half: int, body_hash: str,
                       scheme: str, holder: str) -> BookingResult:
    with session_scope() as s:
        # ── ① 占幂等键（唯一约束是最后防线）──
        replay = claim(s, scope=CREATE_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return BookingResult(False, "body_mismatch", status_code=422,
                                     detail=dict(replay.response))
            resp = replay.response
            return BookingResult(True, "replayed",
                                 booking_id=resp.get("booking_id"),
                                 booking_no=resp.get("booking_no", ""),
                                 status_code=replay.status_code)

        # ── ② 外键存在性先校验：把 500（FK violation）变成 404 ──
        _ensure_refs(s, project_id=project_id, vehicle_id=vehicle_id, testers_id=testers_id)

        # ── ③ 按需物化档期行（并发安全，见模块头部第 3 条）──
        s.execute(text("""
            INSERT INTO vehicle_slots (vehicle_id, slot_date, half, status)
            VALUES (:v, :d, :h, 'free')
            ON CONFLICT (vehicle_id, slot_date, half) DO NOTHING
        """), {"v": vehicle_id, "d": day, "h": half})

        # ── ④ 原子占档期（核心）──
        slot_id = _acquire_slot(s, vehicle_id=vehicle_id, day=day, half=half,
                                scheme=scheme)
        if slot_id is None:
            raise SlotNotAvailable(describe_slot(vehicle_id, day, half))

        # ── ⑤ 建预约单并回填档期 ──
        booking_no = f"BK{uuid.uuid4().hex[:12].upper()}"
        booking_id = s.execute(text("""
            INSERT INTO bookings (booking_no, project_id, vehicle_id, testers_id,
                                  slot_date, half, status)
            VALUES (:no, :pid, :v, :t, :d, :h, 'active')
            RETURNING id
        """), {"no": booking_no, "pid": project_id, "v": vehicle_id,
               "t": testers_id, "d": day, "h": half}).scalar_one()

        s.execute(text("UPDATE vehicle_slots SET booking_id = :b WHERE id = :sid"),
                  {"b": booking_id, "sid": slot_id})

        # ── ⑥ 幂等键记下首次响应（与上面同一事务）──
        store(s, scope=CREATE_SCOPE, key=idem_key, status_code=201,
              response={"booking_id": booking_id, "booking_no": booking_no})

    return BookingResult(True, "created", booking_id=booking_id,
                         booking_no=booking_no, status_code=201)


# ────────────────────────── 取消 ──────────────────────────
def cancel_booking(*, idem_key: str, booking_no: str) -> BookingResult:
    """取消预约并释放档期。

    幂等语义分两层：
      · 同 key 重放 → `replayed`（原样返回首次结果）；
      · 不同 key 但单已取消 → `already`（200，**不再写任何状态**）。
    这样"用户狂点两次取消"和"客户端超时重试"都不会写坏数据，
    也不会因为部分唯一索引 `ux_booking_active` 而卡住后续再预约。
    """
    if not idem_key:
        raise ValueError("cancel_booking 必须带幂等键")

    body_hash = body_hash_of(booking_no=booking_no)

    with session_scope() as s:
        replay = claim(s, scope=CANCEL_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return BookingResult(False, "body_mismatch", status_code=422,
                                     detail=dict(replay.response))
            resp = replay.response
            return BookingResult(True, "replayed",
                                 booking_id=resp.get("booking_id"),
                                 booking_no=resp.get("booking_no", ""),
                                 status_code=replay.status_code,
                                 detail={"already": bool(resp.get("already"))})

        row = s.execute(text("""
            SELECT id, vehicle_id, slot_date, half, status
              FROM bookings WHERE booking_no = :no
        """), {"no": booking_no}).first()
        if row is None:
            raise BookingNotFound(booking_no)

        booking_id, vehicle_id, day, half, current = (
            int(row.id), int(row.vehicle_id), row.slot_date, int(row.half), row.status)

        if current == "cancelled":
            # 另一张 key 重复取消：幂等，不写状态、不重复释放
            payload = {"booking_id": booking_id, "booking_no": booking_no, "already": True}
            store(s, scope=CANCEL_SCOPE, key=idem_key, status_code=200, response=payload)
            return BookingResult(True, "already", booking_id=booking_id,
                                 booking_no=booking_no, status_code=200,
                                 detail={"already": True})

        # 条件更新：只有 active 才能取消；affected=0 说明状态已被并发改掉
        cancelled = s.execute(text("""
            UPDATE bookings SET status = 'cancelled'
             WHERE id = :id AND status = 'active'
            RETURNING id
        """), {"id": booking_id}).scalars().first()
        if cancelled is None:
            raise BookingNotCancellable(booking_no, current)

        # 释放档期：booking_id 守卫保证不会误放别人的档期
        s.execute(text("""
            UPDATE vehicle_slots
               SET status = 'free', booking_id = NULL, version = version + 1, updated_at = now()
             WHERE vehicle_id = :v AND slot_date = :d AND half = :h AND booking_id = :bid
        """), {"v": vehicle_id, "d": day, "h": half, "bid": booking_id})

        payload = {"booking_id": booking_id, "booking_no": booking_no, "already": False}
        store(s, scope=CANCEL_SCOPE, key=idem_key, status_code=200, response=payload)

    # C 方案的 Redis 预占要一起清掉：取消是数据库层的确定事实，
    # 残留的预占会挡住后续合法预约（直到 TTL）——"取消了却约不回来"是最难查的那种 bug。
    redis_lock.force_release(vehicle_id=vehicle_id, slot_date=str(day), half=half)

    return BookingResult(True, "cancelled", booking_id=booking_id,
                         booking_no=booking_no, status_code=200,
                         detail={"already": False})


# ────────────────────────── 查询 ──────────────────────────
def list_slots(*, vin: str, day: str | date_cls, only_free: bool = False) -> Dict[str, Any]:
    """查某台车某天的档期。`only_free=True` 时只返回可约的（下拉框用）。"""
    parsed = parse_day(day)
    with session_scope() as s:
        vehicle = s.execute(text("SELECT id, vin, model FROM vehicles WHERE vin = :v"),
                            {"v": vin}).first()
        if vehicle is None:
            raise RefNotFound("vehicle", vin)

        where = "vehicle_id = :vid AND slot_date = :d"
        if only_free:
            where += " AND status = 'free'"

        rows = s.execute(text(f"""
            SELECT id, slot_date, half, status, booking_id, version
              FROM vehicle_slots
             WHERE {where}
             ORDER BY half
        """), {"vid": int(vehicle.id), "d": parsed}).all()

    slots: List[Dict[str, Any]] = [
        {
            "slot_id": int(r.id),
            "slot_date": str(r.slot_date),
            "half": int(r.half),
            "status": r.status,
            "booking_id": r.booking_id,
            "version": int(r.version),
        }
        for r in rows
    ]
    return {
        "vin": vehicle.vin,
        "model": vehicle.model,
        "slot_date": str(parsed),
        "only_free": only_free,
        "slots": slots,
        "free_count": sum(1 for x in slots if x["status"] == "free"),
    }


# ────────────────────────── 内部 ──────────────────────────
def _acquire_slot(session: Any, *, vehicle_id: int, day: date_cls, half: int,
                  scheme: str) -> Optional[int]:
    """占档期，返回 slot_id；返回 None 表示抢不到（调用方抛 SlotNotAvailable）。

    三种方案在这里分叉，**后面的建单、回填、幂等写入完全一样**：
    这样 bench 对比出来的是"抢占手段"的差异，而不是"两套业务代码"的差异。
    """
    params = {"v": vehicle_id, "d": day, "h": half}

    if scheme == "pessimistic":
        # B：先 SELECT ... FOR UPDATE 锁住这一行，再判断/更新。
        # 适合"先查后改"的复杂逻辑；单档期场景下它就是白白多锁一步（P99 会更高）。
        row = session.execute(text("""
            SELECT id, status FROM vehicle_slots
             WHERE vehicle_id = :v AND slot_date = :d AND half = :h
             FOR UPDATE
        """), params).first()
        if row is None or str(row.status) != "free":
            return None
        session.execute(text("""
            UPDATE vehicle_slots
               SET status = 'booked', version = version + 1, updated_at = now()
             WHERE id = :sid
        """), {"sid": int(row.id)})
        return int(row.id)

    # A（默认）与 C 的落库部分：一条条件 UPDATE，用 affected rows 定胜负
    return session.execute(text("""
        UPDATE vehicle_slots
           SET status = 'booked', version = version + 1, updated_at = now()
         WHERE vehicle_id = :v AND slot_date = :d AND half = :h AND status = 'free'
        RETURNING id
    """), params).scalars().first()


def _ensure_refs(session: Any, *, project_id: int, vehicle_id: int, testers_id: int) -> None:
    """三个外键各查一次主键。表名是代码里的字面量，不含任何用户输入。"""
    checks = (("project", "projects", project_id),
              ("vehicle", "vehicles", vehicle_id),
              ("tester", "testers", testers_id))
    for kind, table, ident in checks:
        found = session.execute(text(f"SELECT 1 FROM {table} WHERE id = :i"),
                                {"i": ident}).first()
        if found is None:
            raise RefNotFound(kind, ident)
