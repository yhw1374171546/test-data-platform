"""认证接口：换 token + 查自己。

`POST /v1/auth/token` 是**唯一不需要 token 的业务接口**（否则没法冷启动）。
它的错误体也遵守统一契约，但故意**不区分**"用户不存在"和"密码错"——
否则这个接口就成了用户名枚举器。
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Dict

from fastapi import APIRouter, Depends, Response

from app.api.authz import current_user
from app.api.errors import translate_domain_errors
from app.api.schemas import TokenRequest
from app.infra import auth, metrics
from app.service import users

logger = logging.getLogger("tdp.auth")

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/token", summary="用账号口令换 JWT")
def issue_token(body: TokenRequest) -> Dict[str, Any]:
    with translate_domain_errors():
        user = users.authenticate(body.username, body.password)
    token, lifetime = auth.issue_token(user.username, user.role)
    metrics.AUTH_ATTEMPTS.labels(result="login_ok").inc()
    logger.info("签发 token username=%s role=%s ttl=%ss", user.username, user.role, lifetime)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": lifetime,
        "username": user.username,
        "role": user.role,
    }


@router.get("/me", summary="当前身份（token 自述，不查库）")
def me(user: Annotated[auth.User, Depends(current_user)],
       response: Response) -> Dict[str, Any]:
    # 让调用方能区分"我是谁"和"这个 token 还剩多久"；剩余时间不查库，按 TTL 反推不划算，
    # 所以只回角色，过期时间交给客户端自己看 exp。
    response.headers["X-Auth-Role"] = user.role
    return {"username": user.username, "role": user.role,
            "auth_enabled": auth.auth_enabled()}
