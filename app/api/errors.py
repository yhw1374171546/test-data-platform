"""错误约定：**所有非 2xx 都是同一个 JSON 形状** `{error, detail, trace_id}`（技术方案 §三）。

为什么必须统一：
调用方（另一个服务、前端、压测脚本）只有拿到稳定的错误体，才能写
"if error == 'slot_not_available' 就换档期，否则告警"这类分支。
一旦有的地方回 `{"detail": "..."}`、有的地方回 FastAPI 默认的 `{"detail": [...]}`，
错误处理就得靠猜 HTTP 状态码，久了必然出 bug。

分层：
  · 领域/服务层抛**业务异常**（SlotNotAvailable、IllegalTransition…），不认识 HTTP；
  · 本模块集中把它们翻译成 `AppError(status_code, error, detail)`；
  · 这里注册的处理器统一渲染成上面的 JSON 形状。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.context import current_trace_id
from app.domain.defect_state import (STATUSES, IllegalTransition,
                                     UnknownStatus)
from app.domain.queue_state import UnknownQueueStatus
from app.service.booking import (BookingNotCancellable, BookingNotFound,
                                 RefNotFound, SlotNotAvailable)
from app.service.claim import QueueConflict, QueueTaskNotFound
from app.service.defect import (BadCursor, DefectNotFound, StateConflict)

logger = logging.getLogger("tdp.error")


class AppError(Exception):
    """带 HTTP 语义的应用异常。`detail` 允许是 dict（结构化信息更好排查）。"""

    def __init__(self, status_code: int, error: str, detail: Any = "",
                 headers: Optional[Dict[str, str]] = None):
        self.status_code = int(status_code)
        self.error = error
        self.detail = detail
        self.headers = headers or {}
        super().__init__(f"{self.status_code} {self.error}: {detail}")


def error_body(error: str, detail: Any = "", status_code: int = 400) -> Dict[str, Any]:
    return {"error": error, "detail": detail, "trace_id": current_trace_id()}


# ────────────────────────── 业务异常 → HTTP ──────────────────────────
@contextmanager
def translate_domain_errors() -> Iterator[None]:
    """包住服务层调用，把业务异常翻译成 AppError。

    用法：
        with translate_domain_errors():
            res = create_booking(...)

    注意这里只做"翻译"，不吞异常：非业务异常（比如连接断了）会原样抛出去，
    由中间件变成 500 —— 500 永远代表"我们自己的问题"，不该被伪装成 409。
    """
    try:
        yield
    except SlotNotAvailable as exc:
        raise AppError(409, "slot_not_available", {"reason": str(exc)}) from exc
    except BookingNotCancellable as exc:
        raise AppError(409, "booking_not_cancellable",
                       {"booking_no": exc.booking_no, "status": exc.status}) from exc
    except BookingNotFound as exc:
        raise AppError(404, "booking_not_found", {"booking_no": exc.booking_no}) from exc
    except DefectNotFound as exc:
        raise AppError(404, "defect_not_found", {"defect_no": exc.defect_no}) from exc
    except QueueTaskNotFound as exc:
        raise AppError(404, "task_not_found", {"task_id": exc.task_id}) from exc
    except QueueConflict as exc:
        # 409 的四种 code 都在这里统一出口：already_claimed / version_conflict /
        # not_claim_owner / not_releasable。接入层不需要知道具体是哪个，调用方按 code 分支。
        raise AppError(409, exc.code, exc.detail) from exc
    except RefNotFound as exc:
        raise AppError(404, f"{exc.kind}_not_found",
                       {"kind": exc.kind, "id": exc.ident}) from exc
    except IllegalTransition as exc:
        raise AppError(409, "illegal_transition", {
            "from": exc.from_status,
            "to": exc.to_status,
            "allowed": exc.allowed,
            "hint": "状态机不允许跳步；请按 allowed 列表逐步推进",
        }) from exc
    except StateConflict as exc:
        raise AppError(409, "state_conflict", {
            "defect_no": exc.defect_no,
            "expected_from": exc.expected_from,
            "found": exc.found,
            "hint": "状态已被并发修改，请重新读取后再试",
        }) from exc
    except UnknownStatus as exc:
        raise AppError(400, "unknown_status",
                       {"status": exc.status, "known": list(STATUSES)}) from exc
    except UnknownQueueStatus as exc:
        from app.domain.queue_state import QUEUE_STATUSES
        raise AppError(400, "unknown_status",
                       {"status": exc.status, "known": list(QUEUE_STATUSES)}) from exc
    except BadCursor as exc:
        raise AppError(400, "invalid_cursor", {"reason": str(exc)}) from exc
    except ValueError as exc:                 # 日期/半天等入参解析失败
        raise AppError(400, "invalid_argument", {"reason": str(exc)}) from exc


# ────────────────────────── 处理器注册 ──────────────────────────
def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(error_body(exc.error, exc.detail, exc.status_code),
                            status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 默认回 {"detail": [...]}；这里改成统一形状，并抹平 pydantic 的原始结构。
        # 只保留字段/原因/类型三项：原始 errors() 里可能带不可 JSON 序列化的 ctx。
        problems: List[Dict[str, str]] = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(error_body("validation_error", problems, 422), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        names = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized",
                 403: "forbidden", 429: "too_many_requests"}
        body = error_body(names.get(exc.status_code, "http_error"), str(exc.detail))
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:   # pragma: no cover
        logger.exception("未处理异常 trace_id=%s", current_trace_id())
        return JSONResponse(error_body("internal", "服务内部错误，请带上 trace_id 反馈"),
                            status_code=500)
