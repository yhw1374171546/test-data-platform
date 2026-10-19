"""日志配置：默认还是人看的文本，线上可切 JSON。

为什么要有这个：
文本日志本地舒服，但在线上"一条请求跨了几个动作"只能靠 grep。
JSON 一行一条、字段固定，配合 trace_id 才能被日志系统（Loki/ELK）直接聚合。
切换只用一个环境变量 `TDP_LOG_FORMAT=json`，不改代码 —— 本地和线上不用两套。

trace_id 从 contextvars 里取（`app.api.context`），不需要每个调用点手动传参。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 标准库 LogRecord 自带这些键，其余键就是调用方传的 extra（要一起带进 JSON）
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


def _current_trace_id() -> str:
    """延迟导入：logs 会被 app 和 scripts 同时引用，避免循环导入。"""
    try:
        from app.api.context import current_trace_id

        return current_trace_id() or ""
    except Exception:                                    # noqa: BLE001 - 日志不能因取 trace 失败而挂
        return ""


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象。字段固定，方便按 trace_id 聚合。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = _current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        # 调用方 logging.info("...", extra={"path": ...}) 的字段一并带上
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup(level: Optional[str] = None, fmt: Optional[str] = None) -> str:
    """初始化根 logger，返回最终使用的格式（json / text），方便启动时打一行。"""
    level = (level or os.environ.get("TDP_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("TDP_LOG_FORMAT") or "text").lower()

    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
        fmt = "text"

    root = logging.getLogger()
    root.handlers.clear()                # 幂等：重复调用不会叠加 handler
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    return fmt
