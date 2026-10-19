"""问题单：提报 + 状态转移（含审计）+ keyset 分页查询。

三个要害，都在这个文件里解决：

1. **合法性判定来自纯函数**（app/domain/defect_state.py），这里只负责"把它落到事务里"。
   非法转移必须让整个事务回滚——包括刚占的幂等键，这样调用方改对参数后能立刻重试。

2. **并发下的"同一目标只写一次审计"**
   先 SELECT 读当前状态做合法性判断，再 `UPDATE ... WHERE id=? AND status=?`。
   条件 UPDATE 才是真正的守卫：如果读到的状态在两次语句之间被改掉了，
   affected=0，此时**不写审计**，而是重新读一次：
     · 已经是目标状态 → 这是并发重复提交，回 `already=true`（200）
     · 是别的状态     → 回 409 `state_conflict`（让调用方重读，而不是静默成功）
   注意这里刻意**不用 `SELECT ... FOR UPDATE`**：单行更新本身就有行锁，
   额外加锁只会让 20 并发排队变慢，还容易和"先读后判"的顺序搞混。

3. **`already=true` 不写审计**
   重复提交同一个 to_status 是幂等重放，不是一次真实流转；
   如果写了，审计表会被刷新按钮灌满，反而看不出谁真的改了状态。

创建时也写一条审计（`(new) → open`），这样"某单是谁提的"不用回查业务表。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import text

from app.domain.defect_state import (IllegalTransition, UnknownStatus,
                                     allowed_next, can_transition, is_known,
                                     normalize)
from app.infra.db import session_scope
from app.infra.idempotency import body_hash_of, claim, store

CREATE_SCOPE = "create_defect"
TRANSITION_SCOPE = "defect_transition"

# 创建时写入审计的"起点状态"。用括号包起来是为了和真实状态名区分开，
# 一眼能看出"这不是一次状态转移，而是对象诞生"。
NEW_STATUS = "(new)"


class DefectNotFound(Exception):
    def __init__(self, defect_no: str):
        self.defect_no = defect_no
        super().__init__(f"问题单不存在：{defect_no!r}")


class StateConflict(Exception):
    """读到的状态在写入前被别人改掉了，且改成了别的状态。让调用方重读后再试。"""

    def __init__(self, defect_no: str, expected_from: str, found: str):
        self.defect_no = defect_no
        self.expected_from = expected_from
        self.found = found
        super().__init__(f"问题单 {defect_no} 状态已变为 {found}（本次基于 {expected_from} 判定）")


class BadCursor(Exception):
    """keyset 游标格式不对 → 400，而不是让 SQL 报错变 500。"""


@dataclass
class DefectResult:
    ok: bool
    code: str                     # created | replayed | body_mismatch
    defect_id: Optional[int] = None
    defect_no: str = ""
    status: str = ""
    status_code: int = 200
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TransitionResult:
    ok: bool
    code: str                     # moved | already | replayed | body_mismatch
    defect_no: str = ""
    from_status: str = ""
    to_status: str = ""
    already: bool = False
    status_code: int = 200
    detail: Dict[str, Any] = field(default_factory=dict)


# ────────────────────────── 创建 ──────────────────────────
def create_defect(*, idem_key: str, title: str, severity: str, project_id: int,
                  actor: str = "system", owner_id: Optional[int] = None,
                  execution_id: Optional[int] = None) -> DefectResult:
    """人工提报一条问题单（正常链路是执行失败时自动带出，见 week 3 的 /v1/executions）。"""
    if not idem_key:
        raise ValueError("create_defect 必须带幂等键")

    title = (title or "").strip()
    if not title:
        raise ValueError("问题单标题不能为空")

    actor = (actor or "").strip() or "system"
    body_hash = body_hash_of(title=title, severity=severity, project_id=project_id,
                             owner_id=owner_id, execution_id=execution_id, actor=actor)

    with session_scope() as s:
        replay = claim(s, scope=CREATE_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return DefectResult(False, "body_mismatch", status_code=422,
                                    detail=dict(replay.response))
            resp = replay.response
            return DefectResult(True, "replayed",
                                defect_id=resp.get("defect_id"),
                                defect_no=resp.get("defect_no", ""),
                                status=resp.get("status", ""),
                                status_code=replay.status_code)

        # 外键显式校验：把 FK violation（500）变成 404
        _ensure_refs(s, project_id=project_id, owner_id=owner_id, execution_id=execution_id)

        defect_id, defect_no = insert_defect(
            s, title=title, severity=severity, project_id=project_id,
            actor=actor, owner_id=owner_id, execution_id=execution_id)

        payload = {"defect_id": defect_id, "defect_no": defect_no, "status": "open"}
        store(s, scope=CREATE_SCOPE, key=idem_key, status_code=201, response=payload)

    return DefectResult(True, "created", defect_id=defect_id, defect_no=defect_no,
                        status="open", status_code=201)


def insert_defect(session: Any, *, title: str, severity: str, project_id: int,
                  actor: str = "system", owner_id: Optional[int] = None,
                  execution_id: Optional[int] = None) -> Tuple[int, str]:
    """在**调用方的事务里**建一条问题单（含 `(new) → open` 审计），返回 (id, no)。

    为什么抽出来：提交执行结果时"失败自动带出缺陷"必须和 executions / outbox
    **在同一个事务**里（否则会出现"结果记了、缺陷没建"或反过来）。
    而 `create_defect` 自己开事务、自己占幂等键，不能在被包裹的事务里调用。
    这里只做插入，不 commit、不碰幂等键——由调用方决定事务边界。
    """
    defect_no = f"DF{uuid.uuid4().hex[:12].upper()}"
    defect_id = session.execute(text("""
        INSERT INTO defects (defect_no, title, severity, status, project_id,
                             execution_id, owner_id)
        VALUES (:no, :title, :sev, 'open', :pid, :eid, :oid)
        RETURNING id
    """), {"no": defect_no, "title": title, "sev": severity, "pid": project_id,
           "eid": execution_id, "oid": owner_id}).scalar_one()

    # 创建也是审计的一部分：谁提的、什么时候、为什么（reason=提报来源）
    session.execute(text("""
        INSERT INTO defect_transitions (defect_id, from_status, to_status, actor, reason)
        VALUES (:d, :frm, 'open', :actor, :reason)
    """), {"d": defect_id, "frm": NEW_STATUS, "actor": actor,
           "reason": "人工提报" if execution_id is None
                     else f"执行记录 {execution_id} 提报"})
    return int(defect_id), defect_no


# ────────────────────────── 状态转移 ──────────────────────────
def transition_defect(*, idem_key: str, defect_no: str, to_status: str,
                      actor: str = "system", reason: str = "") -> TransitionResult:
    """把问题单推到 `to_status`。

    返回（而不是抛异常）的三种情况：moved / already / replayed / body_mismatch。
    抛异常的两种情况：DefectNotFound（404）、IllegalTransition（409）、StateConflict（409）。
    抛异常意味着事务回滚、幂等键不留痕——这是有意的，见模块头部第 1 条。
    """
    if not idem_key:
        raise ValueError("transition_defect 必须带幂等键")

    actor = (actor or "").strip() or "system"
    reason = (reason or "").strip()
    target = str(to_status or "").strip().lower()

    if not is_known(target):
        # 状态名写错属于参数错误，必须在 claim 之前拒掉：
        # 一旦先占了幂等键，哪怕立刻返回，session_scope 也会把键提交掉，
        # 于是"把 to_status 改对再用同 key 重试"会被判成 body_mismatch(422)——
        # 调用方会以为是自己业务冲突，其实只是刚才敲错了一个字。
        raise UnknownStatus(str(to_status))

    body_hash = body_hash_of(defect_no=defect_no, to_status=target,
                             actor=actor, reason=reason)

    with session_scope() as s:
        replay = claim(s, scope=TRANSITION_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return TransitionResult(False, "body_mismatch", defect_no=defect_no,
                                        to_status=target, status_code=422,
                                        detail=dict(replay.response))
            resp = replay.response
            return TransitionResult(True, "replayed", defect_no=defect_no,
                                    from_status=resp.get("from_status", ""),
                                    to_status=resp.get("to_status", target),
                                    already=bool(resp.get("already")),
                                    status_code=replay.status_code)

        row = s.execute(text("SELECT id, status FROM defects WHERE defect_no = :no"),
                        {"no": defect_no}).first()
        if row is None:
            raise DefectNotFound(defect_no)

        defect_id, current = int(row.id), normalize(row.status)

        if current == target:
            # 幂等：重复提交同一目标状态。不写审计、不改 updated_at。
            payload = {"from_status": current, "to_status": current, "already": True}
            store(s, scope=TRANSITION_SCOPE, key=idem_key, status_code=200, response=payload)
            return TransitionResult(True, "already", defect_no=defect_no,
                                    from_status=current, to_status=current,
                                    already=True, status_code=200,
                                    detail={"allowed_next": sorted(allowed_next(current))})

        # 纯函数判定；非法则抛异常 → 事务回滚（幂等键一起回滚）
        if not can_transition(current, target):
            raise IllegalTransition(current, target)

        # 条件 UPDATE：才是真正的守卫。affected=0 表示状态在读取之后被别人改掉了。
        moved = s.execute(text("""
            UPDATE defects SET status = :to, updated_at = now()
             WHERE id = :id AND status = :frm
            RETURNING id
        """), {"id": defect_id, "to": target, "frm": current}).scalars().first()

        if moved is None:
            found = s.execute(text("SELECT status FROM defects WHERE id = :id"),
                              {"id": defect_id}).scalar_one()
            if found == target:
                # 并发重复提交：别人先推到了同一目标 → 等价于 already
                payload = {"from_status": found, "to_status": found, "already": True}
                store(s, scope=TRANSITION_SCOPE, key=idem_key, status_code=200, response=payload)
                return TransitionResult(True, "already", defect_no=defect_no,
                                        from_status=found, to_status=found,
                                        already=True, status_code=200)
            raise StateConflict(defect_no, current, found)

        audit_id = s.execute(text("""
            INSERT INTO defect_transitions (defect_id, from_status, to_status, actor, reason)
            VALUES (:d, :frm, :to, :actor, :reason)
            RETURNING id
        """), {"d": defect_id, "frm": current, "to": target,
               "actor": actor, "reason": reason}).scalar_one()

        payload = {"from_status": current, "to_status": target,
                   "already": False, "audit_id": audit_id}
        store(s, scope=TRANSITION_SCOPE, key=idem_key, status_code=200, response=payload)

    return TransitionResult(True, "moved", defect_no=defect_no, from_status=current,
                            to_status=target, already=False, status_code=200,
                            detail={"audit_id": audit_id})


# ────────────────────────── 查询 ──────────────────────────
def get_defect(defect_no: str) -> Dict[str, Any]:
    """单条详情 + 完整审计轨迹（按时间正序，便于前端画时间线）。"""
    with session_scope() as s:
        row = s.execute(text("""
            SELECT id, defect_no, title, severity, status, project_id,
                   execution_id, owner_id, created_at, updated_at
              FROM defects WHERE defect_no = :no
        """), {"no": defect_no}).first()
        if row is None:
            raise DefectNotFound(defect_no)

        trails = s.execute(text("""
            SELECT from_status, to_status, actor, reason, created_at
              FROM defect_transitions
             WHERE defect_id = :d
             ORDER BY id
        """), {"d": int(row.id)}).all()

    return {
        **_defect_dict(row),
        "allowed_next": sorted(allowed_next(row.status)),
        "transitions": [
            {
                "from_status": t.from_status,
                "to_status": t.to_status,
                "actor": t.actor,
                "reason": t.reason,
                "created_at": t.created_at,
            }
            for t in trails
        ],
    }


def list_defects(*, project_id: Optional[int] = None,
                 project_ids: Optional[Set[int]] = None,
                 status: Optional[str] = None,
                 limit: int = 20, cursor: Optional[str] = None) -> Dict[str, Any]:
    """keyset 分页（技术方案 §4.7 的接口形态）。

    `project_ids` 是 **ABAC 的可见范围**：
      · `None`  → 不按项目过滤（管理员）
      · `set()` → 一个项目都没有，返回空页（**必须真的过滤成空**，
                  写成"空就不过滤"是 ABAC 最典型的漏洞）
      · 非空   → 只返回这些项目的数据

    游标 = `(created_at, id)`：
      · 只带 created_at 会在同一毫秒的多条记录上丢数据；
      · 带上 id 才是全序，且能直接命中 `ix_defects_keyset (created_at DESC, id DESC)`。
    """
    limit = max(1, min(int(limit or 20), 100))
    where: List[str] = ["1 = 1"]
    params: Dict[str, Any] = {}

    if project_id is not None:
        where.append("project_id = :pid")
        params["pid"] = int(project_id)

    if project_ids is not None:
        if not project_ids:
            return {"items": [], "next_cursor": None, "limit": limit, "has_more": False}
        where.append("project_id = ANY(:pids)")
        params["pids"] = [int(p) for p in sorted(project_ids)]

    if status:
        if not is_known(str(status).strip().lower()):
            raise UnknownStatus(str(status))
        where.append("status = :st")
        params["st"] = str(status).strip().lower()

    if cursor:
        params.update(_parse_cursor(cursor))
        where.append("(created_at, id) < (:cts, :cid)")

    # 多取一条用于判断"是否还有下一页"，避免为了翻页跑一次 count(*)
    params["lim"] = limit + 1

    sql = f"""
        SELECT id, defect_no, title, severity, status, project_id,
               execution_id, owner_id, created_at, updated_at
          FROM defects
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC, id DESC
         LIMIT :lim
    """
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_defect_dict(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}|{int(last.id)}"

    return {"items": items, "next_cursor": next_cursor, "limit": limit,
            "has_more": has_more}


# ────────────────────────── 内部 ──────────────────────────
def _defect_dict(row: Any) -> Dict[str, Any]:
    return {
        "defect_id": int(row.id),
        "defect_no": row.defect_no,
        "title": row.title,
        "severity": row.severity,
        "status": row.status,
        "project_id": int(row.project_id),
        "execution_id": row.execution_id,
        "owner_id": row.owner_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _parse_cursor(cursor: str) -> Dict[str, Any]:
    """游标是 `ISO时间|id`。解析失败必须是 400，不能让 PG 时报错。"""
    try:
        ts_raw, id_raw = str(cursor).rsplit("|", 1)
        ts: datetime = datetime.fromisoformat(ts_raw)
        cid = int(id_raw)
    except (ValueError, AttributeError) as exc:
        raise BadCursor(f"游标格式应为 'ISO时间|id'：{cursor!r}") from exc
    return {"cts": ts, "cid": cid}


def _ensure_refs(session: Any, *, project_id: int,
                 owner_id: Optional[int], execution_id: Optional[int]) -> None:
    from app.service.booking import RefNotFound       # 复用同一套 404 语义

    checks: List[tuple] = [("project", "projects", project_id)]
    if owner_id is not None:
        checks.append(("tester", "testers", owner_id))
    if execution_id is not None:
        checks.append(("execution", "executions", execution_id))
    for kind, table, ident in checks:
        if session.execute(text(f"SELECT 1 FROM {table} WHERE id = :i"),
                           {"i": ident}).first() is None:
            raise RefNotFound(kind, ident)
