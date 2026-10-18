"""FastAPI 依赖：幂等键头、trace_id。"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Header

from app.api.errors import AppError


def require_idempotency_key(
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key")] = None,
) -> str:
    """写接口必须带 `Idempotency-Key`（技术方案 §三：预约/领取/提交/转移均为必需）。

    为什么做成 400 而不是给个默认值：
    幂等键是**调用方的承诺**——由调用方生成并在重试时复用。
    服务端替它编一个，就等于每次重试都是新请求，"同 key 只生效一次"永远测不出来。
    """
    key = (idempotency_key or "").strip()
    if not key:
        raise AppError(400, "missing_idempotency_key",
                       {"hint": "写接口必须带 Idempotency-Key 头；同一次重试请复用同一个值"})
    if len(key) > 128:
        raise AppError(400, "invalid_idempotency_key", {"reason": "长度不能超过 128"})
    return key
