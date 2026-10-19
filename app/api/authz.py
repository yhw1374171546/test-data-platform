"""HTTP 鉴权依赖：从 `Authorization: Bearer <token>` 解出身份并按角色放行。

放在 api 层（而不是 infra）是因为它要抛 HTTP 语义的 AppError：
infra 层保持"不认识 HTTP"，这样同一套认证原语可以被命令行复用来发 token。

`require_roles("tester")` 是**至少 tester**：admin 也能过。
角色只有一个维度，够用就好 —— 真要做细粒度权限（"只能改自己项目的数据"），
那属于 ABAC，要靠数据行上的 project_id 过滤，不是一个装饰器能解决的。
"""
from __future__ import annotations

from typing import Annotated, Callable, Optional

from fastapi import Depends, Header, Request

from app.api.errors import AppError
from app.infra import auth, metrics

BEARER_HINT = "Authorization: Bearer <token>"

REASON_TEXT = {
    "missing": "缺少 Authorization 头",
    "expired": "token 已过期，请重新登录",
    "invalid": "token 无效",
    "bad_role": "token 里的角色不被识别",
}


def current_user(authorization: Optional[str] = Header(default=None)) -> auth.User:
    """解析当前身份。`TDP_AUTH=off` 时直接返回匿名管理员（本地演示用）。"""
    if not auth.auth_enabled():
        return auth.ANONYMOUS
    try:
        user = auth.user_from_token(authorization or "")
    except auth.TokenError as exc:
        metrics.AUTH_ATTEMPTS.labels(result=exc.reason).inc()
        raise AppError(
            401, f"unauthorized_{exc.reason}",
            {"reason": REASON_TEXT.get(exc.reason, "认证失败"), "hint": BEARER_HINT},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    metrics.AUTH_ATTEMPTS.labels(result="ok").inc()
    return user


def require_roles(*roles: str) -> Callable[..., auth.User]:
    """生成一个"至少拥有其中某个角色"的依赖。

    多个角色时取**最宽松**的那个（`require_roles("admin", "tester")` == 至少 tester），
    这样写在路由器上不会意外收紧权限。
    """
    if not roles:
        raise ValueError("require_roles 至少要给一个角色")
    needed = min(roles, key=lambda r: auth.ROLE_RANK.get(r, 99))
    return _guard(needed)


READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_read_write(*, read: str = "viewer", write: str = "tester") -> Callable[..., auth.User]:
    """按 HTTP 方法分级：读给 `read`，写给 `write`。

    为什么需要这个：一个路由器里通常既有 `GET /v1/defects` 也有 `POST /v1/defects`。
    如果整个路由器只挂一个角色，要么 viewer 读不了列表（太紧），
    要么 viewer 能提单（太松）。按方法分是最小代价的细分。
    """
    def guard(request: Request,
              user: Annotated[auth.User, Depends(current_user)]) -> auth.User:
        needed = read if request.method in READ_METHODS else write
        if not auth.allows(user.role, needed):
            metrics.AUTH_ATTEMPTS.labels(result="forbidden").inc()
            raise AppError(403, "forbidden", {
                "need": needed,
                "role": user.role,
                "method": request.method,
                "hint": f"{request.method} 接口需要 {needed} 及以上角色",
            })
        return user

    return guard


def _guard(needed: str) -> Callable[..., auth.User]:
    def guard(user: Annotated[auth.User, Depends(current_user)]) -> auth.User:
        if not auth.allows(user.role, needed):
            metrics.AUTH_ATTEMPTS.labels(result="forbidden").inc()
            raise AppError(403, "forbidden", {
                "need": needed,
                "role": user.role,
                "hint": f"该接口需要 {needed} 及以上角色",
            })
        return user

    return guard
