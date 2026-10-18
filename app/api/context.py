"""请求级上下文：trace_id 的读写。

用 ContextVar 而不是把 trace_id 一路当参数透传：
① 中间件里 set 一次，异常处理器、日志、错误体都能拿到；
② 同步 endpoint 跑在线程池里，ContextVar 依然跟随该请求的执行上下文，
   不像 threading.local 那样在池化线程复用时会串请求（这是真实踩过的坑）。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_TRACE_ID: ContextVar[str] = ContextVar("tdp_trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(value: str) -> Token:
    return _TRACE_ID.set(value or "")


def reset_trace_id(token: Token) -> None:
    try:
        _TRACE_ID.reset(token)
    except ValueError:                                  # pragma: no cover - 跨上下文复位
        _TRACE_ID.set("")


def current_trace_id() -> str:
    return _TRACE_ID.get() or "-"
