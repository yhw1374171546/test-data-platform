"""账号相关的服务层：只用 DB + 认证原语，不认识 HTTP。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.infra.auth import User, hash_password, verify_password
from app.infra.db import session_scope

logger = logging.getLogger("tdp.users")


class AuthFailed(Exception):
    """登录失败。`reason` 只用于**响应体**，不要回显"用户不存在还是密码错"。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def authenticate(username: str, password: str) -> User:
    """校验账号口令。**失败信息对外统一为 invalid_credentials**。

    为什么区分 reason 但不对外暴露：
    日志里要能查"是用户不存在还是密码错"，但响应必须一致，
    否则接口就成了用户名枚举器。
    """
    name = (username or "").strip()
    if not name or not password:
        raise AuthFailed("bad_request", "用户名和密码都要填")
    with session_scope() as s:
        row = s.execute(text("""
            SELECT username, password_hash, role, active FROM app_users WHERE username = :u
        """), {"u": name}).first()
    if row is None:
        logger.info("登录失败：用户不存在 username=%s", name)
        raise AuthFailed("unknown_user")
    if not row.active:
        logger.info("登录失败：账号已停用 username=%s", name)
        raise AuthFailed("inactive")
    if not verify_password(password, row.password_hash):
        logger.info("登录失败：口令不对 username=%s", name)
        raise AuthFailed("bad_password")
    return User(username=str(row.username), role=str(row.role))


def get_user(username: str) -> Optional[Dict[str, Any]]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT username, role, active, created_at FROM app_users WHERE username = :u
        """), {"u": username}).first()
    return dict(row._mapping) if row else None


def create_user(username: str, password: str, role: str = "viewer") -> Dict[str, Any]:
    """建账号（幂等：同名已存在就重置口令和角色）。给 init_db 和运维脚本用。"""
    hashed = hash_password(password)
    with session_scope() as s:
        row = s.execute(text("""
            INSERT INTO app_users (username, password_hash, role, active)
            VALUES (:u, :h, :r, TRUE)
            ON CONFLICT (username) DO UPDATE
               SET password_hash = EXCLUDED.password_hash,
                   role = EXCLUDED.role,
                   active = TRUE
            RETURNING username, role, active
        """), {"u": username, "h": hashed, "r": role}).one()
    return dict(row._mapping)


def set_active(username: str, active: bool) -> bool:
    with session_scope() as s:
        result = s.execute(text("UPDATE app_users SET active = :a WHERE username = :u"),
                           {"a": bool(active), "u": username})
    return bool(result.rowcount)


def list_users() -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT username, role, active, created_at FROM app_users ORDER BY id
        """)).all()
    return [dict(r._mapping) for r in rows]
