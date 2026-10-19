"""认证原语：口令哈希 + JWT 签发/校验。

这一层**不认识 HTTP**：不返回 401、不碰 AppError。HTTP 语义在 `app/api/authz.py`，
这样 `app/infra/auth.py` 既可以被 FastAPI 用，也能被命令行（发 token）用。

口令为什么用 PBKDF2 而不是 bcrypt/argon2：
标准库 `hashlib.pbkdf2_hmac` 就够用，少一个需要编译的依赖 —— 这个项目的原则是
"能在 8 秒内装好的依赖优先"。真上生产我会换 argon2id（内存硬，抗 GPU）。

JWT 的三个决定：
1. **HS256 + 服务端密钥**：单体服务不需要非对称签名，RS256 换来的是"公钥分发"，
   这里没有第三方验签方，纯属加复杂度。
2. **只放 sub/role/exp**：token 是"凭证"不是"会话"，不要往里塞权限清单 ——
   权限变了旧 token 还在用，那是预期行为（短 TTL 兜底），不是 bug。
3. **不做黑名单**：停用账号后已签发的 token 到期前仍然有效。
   要立刻失效就得引入黑名单/版本号，那是另一套东西；这里 TTL 默认 1 小时，够用。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any, Dict, NamedTuple, Optional, Tuple

import jwt

logger = logging.getLogger("tdp.auth")

ALGORITHM = "HS256"
DEFAULT_SECRET = "dev-only-insecure-secret-change-me"
DEFAULT_TTL = 3600
PBKDF2_ROUNDS = 120_000
ROLES = ("admin", "tester", "viewer")
# 角色是"至少"关系：admin > tester > viewer
ROLE_RANK = {"viewer": 1, "tester": 2, "admin": 3}


class User(NamedTuple):
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


ANONYMOUS = User(username="anonymous", role="admin")
"""`TDP_AUTH=off` 时用的身份。

为什么给 admin 而不是 viewer：关掉认证是**明确的运维决定**（本地演示、内网联调），
此时如果只降级到 viewer，管理员接口照样调不通，那就失去了"关掉"的意义。
生产必须 `TDP_AUTH=on` —— 这条写在 README 和启动日志里。
"""


class TokenError(Exception):
    """token 缺失/过期/签名不对/格式错。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ────────────────────────── 配置 ──────────────────────────
def secret() -> str:
    value = (os.environ.get("TDP_JWT_SECRET") or "").strip()
    if value:
        return value
    logger.warning("TDP_JWT_SECRET 未设置，正在使用开发默认密钥 —— 生产环境必须覆盖")
    return DEFAULT_SECRET


def ttl() -> int:
    try:
        return max(1, int(os.environ.get("TDP_JWT_TTL", str(DEFAULT_TTL))))
    except ValueError:
        return DEFAULT_TTL


def auth_enabled() -> bool:
    """每次调用都读环境变量：测试要用 monkeypatch 切换，不能在导入时定死。"""
    return (os.environ.get("TDP_AUTH", "on") or "").strip().lower() not in ("off", "0", "false", "no")


# ────────────────────────── 口令 ──────────────────────────
def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    """返回 `pbkdf2_sha256$轮数$盐$哈希`（盐和轮数一起存，方便以后升级强度）。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较；格式不对一律当失败（不要抛出去，避免把内部格式泄露给调用方）。"""
    try:
        scheme, rounds, salt, expected = (stored or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                     salt.encode("utf-8"), int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


# ────────────────────────── JWT ──────────────────────────
def issue_token(username: str, role: str, *, ttl_seconds: Optional[int] = None,
                now: Optional[int] = None) -> Tuple[str, int]:
    """签发 token，返回 (token, 有效期秒数)。"""
    if role not in ROLE_RANK:
        raise ValueError(f"未知角色：{role!r}")
    lifetime = int(ttl_seconds if ttl_seconds is not None else ttl())
    issued = int(now if now is not None else time.time())
    payload = {"sub": username, "role": role, "iat": issued, "exp": issued + lifetime,
               "jti": secrets.token_hex(8)}
    return jwt.encode(payload, secret(), algorithm=ALGORITHM), lifetime


def decode_token(token: str) -> Dict[str, Any]:
    """校验并解出 payload；任何问题都抛 TokenError，调用方统一翻成 401。"""
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        raise TokenError("missing")
    try:
        payload = jwt.decode(raw, secret(), algorithms=[ALGORITHM],
                             options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid") from exc
    if str(payload.get("role", "")) not in ROLE_RANK:
        raise TokenError("bad_role")
    return payload


def user_from_token(token: str) -> User:
    payload = decode_token(token)
    return User(username=str(payload["sub"]), role=str(payload["role"]))


def allows(role: str, needed: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(needed, 99)
