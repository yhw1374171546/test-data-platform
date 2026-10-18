"""FastAPI 入口：中间件（trace_id + 指标）→ 路由 → 运维端点。

中间件里做了三件事，顺序都是刻意的：

1. **trace_id 先落上下文**：之后的日志、错误体、响应头都能带上同一个 ID；
   客户端自带 `X-Request-ID` 就复用，方便跨服务串联。
2. **指标放在 finally**：成功、4xx、5xx 都要计数，否则监控里只剩"成功请求"，
   错误率永远是 0 —— 这种"看起来很好的看板"比没有监控更危险。
3. **异常兜底也在这里**：500 一定要回统一 JSON 形状（带 trace_id），
   不能让 Starlette 默认的纯文本 500 漏给前端。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.bookings import router as bookings_router
from app.api.catalog import router as catalog_router
from app.api.context import (current_trace_id, new_trace_id, reset_trace_id,
                             set_trace_id)
from app.api.defects import router as defects_router
from app.api.errors import install_error_handlers
from app.api.executions import router as executions_router
from app.api.jobs import router as jobs_router
from app.api.queue import router as queue_router
from app.infra import metrics
from app.infra.db import engine

logger = logging.getLogger("tdp.http")

SERVICE = "test-data-platform"
VERSION = "0.1.0"


async def _observe(request: Request, call_next: Any) -> Response:
    trace_id = (request.headers.get("X-Request-ID") or "").strip() or new_trace_id()
    token = set_trace_id(trace_id)
    metrics.HTTP_INFLIGHT.inc()
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = trace_id
        return response
    except Exception:                                   # noqa: BLE001 - 兜底
        logger.exception("未捕获异常 trace_id=%s path=%s", trace_id, request.url.path)
        return JSONResponse(
            {"error": "internal",
             "detail": "服务内部错误，请带上 trace_id 反馈",
             "trace_id": trace_id},
            status_code=500,
            headers={"X-Request-ID": trace_id},
        )
    finally:
        elapsed = time.perf_counter() - started
        # path 用路由模板而不是真实 URL：否则每个 id 都是一条新的时间序列
        route = request.scope.get("route")
        label = getattr(route, "path", None) or request.url.path
        metrics.HTTP_INFLIGHT.dec()
        metrics.HTTP_REQUESTS.labels(method=request.method, path=label,
                                     status=str(status)).inc()
        metrics.HTTP_LATENCY.labels(method=request.method, path=label).observe(elapsed)
        reset_trace_id(token)


def create_app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("TDP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="测试数据管理平台",
        version=VERSION,
        description=(
            "整车测试数据管理后端：车辆档期占用 / 执行任务领取 / 问题单状态机 / 日报汇总与对账。\n\n"
            "**写接口必须带 `Idempotency-Key`**：同 key 同 body 返回首次响应，同 key 换 body 返回 422。\n"
            "错误体统一为 `{error, detail, trace_id}`。"
        ),
    )

    install_error_handlers(app)
    app.middleware("http")(_observe)

    app.include_router(bookings_router)
    app.include_router(defects_router)
    app.include_router(queue_router)
    app.include_router(executions_router)
    app.include_router(jobs_router)
    app.include_router(admin_router)
    app.include_router(catalog_router)

    @app.get("/healthz", tags=["ops"], summary="存活探针（不查库）")
    def healthz() -> Dict[str, str]:
        # liveness 只回答"进程还在不在"。查库会带来误杀：DB 抖动一下就把容器重启了，
        # 重启并不能修好 DB，反而让故障扩大。
        return {"status": "ok", "service": SERVICE}

    @app.get("/readyz", tags=["ops"], summary="就绪探针（查库）")
    def readyz() -> Response:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:                        # noqa: BLE001
            logger.warning("readyz 失败 trace_id=%s err=%s", current_trace_id(), exc)
            return JSONResponse({"status": "degraded", "db": "down",
                                 "error": type(exc).__name__,
                                 "trace_id": current_trace_id()},
                                status_code=503)
        return JSONResponse({"status": "ok", "db": "ok",
                             "service": SERVICE, "version": VERSION})

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标")
    def prometheus_metrics() -> Response:
        if not metrics.EXPORTER_ENABLED:               # pragma: no cover
            return JSONResponse({"error": "metrics_disabled",
                                 "detail": "未安装 prometheus_client",
                                 "trace_id": current_trace_id()}, status_code=501)
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @app.get("/", include_in_schema=False)
    def index() -> Dict[str, Any]:
        return {
            "service": SERVICE,
            "version": VERSION,
            "docs": "/docs",
            "health": ["/healthz", "/readyz"],
            "endpoints": [
                "POST /v1/bookings",
                "POST /v1/bookings/{booking_no}/cancel",
                "GET  /v1/vehicles/{vin}/slots?date=",
                "POST /v1/defects",
                "POST /v1/defects/{defect_no}/transition",
                "GET  /v1/defects",
                "GET  /v1/defects/{defect_no}",
                "GET  /v1/defects/status-machine",
                "GET  /v1/queue",
                "POST /v1/queue",
                "POST /v1/queue/claim",
                "POST /v1/queue/{task_id}/release",
                "POST /v1/executions",
                "POST /v1/jobs/summary",
                "POST /v1/admin/outbox/replay",
                "GET  /v1/admin/outbox",
                "GET  /v1/vehicles/{vin}",
                "PATCH /v1/vehicles/{vin}",
                "GET  /v1/test-cases/{case_no}",
            ],
        }

    return app


app = create_app()
