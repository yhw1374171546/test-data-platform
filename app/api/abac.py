"""ABAC 权限：JWT 回答"你是谁"，角色回答"能做什么动作"，成员关系回答"能碰哪些项目的数据"。

三个维度正交：
  · 认证（JWT）：这个请求是不是已登录的人发的
  · 角色（RBAC）：这个动作（读/写/管理）他有没有权限
  · 项目（ABAC）：这条**具体数据**属于的项目，他在不在里面

只做前两层是很多项目的通病：一个 tester 能改别的项目的档期，因为"他是 tester"。
"""
from __future__ import annotations

from typing import Optional, Set

from app.api.errors import AppError
from app.infra import auth
from app.service import members


def visible_projects(user: auth.User) -> Optional[Set[int]]:
    """列表接口的可见范围。`None` = 不限（admin），空集合 = 一个项目都没有。

    **空集合必须真的过滤成空**：如果实现成"空就不加 where"，那就等于谁都能看全部 ——
    这是 ABAC 最典型的漏洞，所以这里返回 None 和 set() 的语义必须写死。
    """
    if user.role == "admin":
        return None
    return members.projects_of(user.username)


def ensure_project(user: auth.User, project_id: Optional[int], *, action: str = "访问") -> None:
    """写/查单条数据前的校验。admin 直通，其余人必须在该项目成员表里。"""
    if user.role == "admin":
        return
    if project_id is None:
        raise AppError(403, "project_forbidden", {
            "reason": "这条数据不属于任何项目，普通账号不能操作",
            "action": action,
        })
    if not members.is_member(user.username, int(project_id)):
        raise AppError(403, "project_forbidden", {
            "project_id": int(project_id),
            "action": action,
            "hint": "只能操作自己参与的项目；需要加入请联系管理员",
        })


def ensure_task(user: auth.User, task_id: int, *, action: str = "操作") -> None:
    """按 task_id 反查项目再校验。反查是为了不信任客户端传来的 project_id。"""
    project_id = members.project_of_task(int(task_id))
    if project_id is None:
        # 任务不存在交给服务层报 404，这里不抢它的语义
        return
    ensure_project(user, project_id, action=action)


def ensure_defect(user: auth.User, defect_no: str, *, action: str = "操作") -> None:
    project_id = members.project_of_defect(defect_no)
    if project_id is None:
        return
    ensure_project(user, project_id, action=action)


def ensure_booking(user: auth.User, booking_no: str, *, action: str = "操作") -> None:
    project_id = members.project_of_booking(booking_no)
    if project_id is None:
        return
    ensure_project(user, project_id, action=action)
