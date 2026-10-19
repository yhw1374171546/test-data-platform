"""项目成员：ABAC 的数据来源。

为什么权限边界是"项目"而不是"车辆/用例"：
这个系统里所有业务数据都挂在 project 下（档期、任务、执行结果、问题单、日报），
项目也是现实组织里的协作边界（一个车型项目一个团队）。按项目隔离既够用，又好解释。

实现上**只做"能不能碰这个项目"**，不做字段级权限（谁能改哪些字段）。
后者要做的是策略引擎（OPA/Casbin），不是这个体量的服务该背的复杂度。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text

from app.infra.db import session_scope


def projects_of(username: str) -> Set[int]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT project_id FROM project_members WHERE username = :u
        """), {"u": username}).scalars().all()
    return {int(r) for r in rows}


def is_member(username: str, project_id: int) -> bool:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT 1 FROM project_members WHERE username = :u AND project_id = :p
        """), {"u": username, "p": int(project_id)}).first()
    return row is not None


def add_member(project_id: int, username: str) -> None:
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO project_members (project_id, username) VALUES (:p, :u)
            ON CONFLICT (project_id, username) DO NOTHING
        """), {"p": int(project_id), "u": username})


def remove_member(project_id: int, username: str) -> bool:
    with session_scope() as s:
        result = s.execute(text("""
            DELETE FROM project_members WHERE project_id = :p AND username = :u
        """), {"p": int(project_id), "u": username})
    return bool(result.rowcount)


def list_members(project_id: int) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT username, created_at FROM project_members
             WHERE project_id = :p ORDER BY username
        """), {"p": int(project_id)}).all()
    return [dict(r._mapping) for r in rows]


# ────────────────────────── 反查：资源属于哪个项目 ──────────────────────────
# 有些接口拿到的是 task_id / defect_no，而不是 project_id。
# 这时候不能"信任客户端传的 project_id"（那等于没校验），必须自己反查。
def project_of_task(task_id: int) -> Optional[int]:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT project_id FROM execution_queue WHERE id = :t"),
            {"t": int(task_id)}).first()
    return int(row.project_id) if row else None


def project_of_defect(defect_no: str) -> Optional[int]:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT project_id FROM defects WHERE defect_no = :no"),
            {"no": str(defect_no)}).first()
    return int(row.project_id) if row else None


def project_of_booking(booking_no: str) -> Optional[int]:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT project_id FROM bookings WHERE booking_no = :no"),
            {"no": str(booking_no)}).first()
    return int(row.project_id) if row else None
