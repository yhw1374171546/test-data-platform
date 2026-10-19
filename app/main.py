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

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.authz import require_read_write, require_roles
from app.api.bookings import router as bookings_router
from app.api.catalog import router as catalog_router
from app.api.context import (current_trace_id, new_trace_id, reset_trace_id,
                             set_trace_id)
from app.api.defects import router as defects_router
from app.api.errors import install_error_handlers
from app.api.executions import router as executions_router
from app.api.jobs import router as jobs_router
from app.api.meta import router as meta_router
from app.api.queue import router as queue_router
from app.infra import logs, metrics, ratelimit, tracing
from app.infra.db import engine

logger = logging.getLogger("tdp.http")

SERVICE = "test-data-platform"
VERSION = "0.1.0"


async def _observe(request: Request, call_next: Any) -> Response:
    trace_id = (request.headers.get("X-Request-ID") or "").strip() or new_trace_id()
    token = set_trace_id(trace_id)
    metrics.HTTP_INFLIGHT.inc()
    _INFLIGHT["n"] += 1
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = trace_id
        return response
    except Exception as exc:                            # noqa: BLE001 - 兜底
        logger.exception("未捕获异常 trace_id=%s path=%s", trace_id, request.url.path)
        # 异常在这里被"变成 500 响应"，所以这里是唯一能拿到堆栈的地方 ——
        # 记到当前 span 上，追踪系统里才能看到真正的失败原因。
        tracing.record_exception(exc)
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
        _INFLIGHT["n"] = max(0, _INFLIGHT["n"] - 1)
        metrics.HTTP_REQUESTS.labels(method=request.method, path=label,
                                     status=str(status)).inc()
        metrics.HTTP_LATENCY.labels(method=request.method, path=label).observe(elapsed)
        reset_trace_id(token)


async def _trace(request: Request, call_next: Any) -> Response:
    """链路追踪（最外层）：span 覆盖从进来到响应的全过程，并把 traceparent 回写给调用方。

    为什么要回写：调用方（前端/网关/另一个服务）拿到响应头里的 traceparent，
    下次请求带上它，就能把两次调用串成一条 trace —— 这是"跨服务排查"的入口。
    `X-Request-ID` 和它同时存在：前者给人看（日志里能搜），后者给追踪系统拼。
    """
    from opentelemetry import trace as _otel_trace

    parent = tracing.extract(request.headers)
    span = tracing.tracer().start_span(
        f"{request.method} {request.url.path}", context=parent,
        kind=tracing.SpanKind.SERVER,
    )
    span.set_attribute("http.request.method", request.method)
    span.set_attribute("url.path", request.url.path)
    span.set_attribute("tdp.request_id", request.headers.get("X-Request-ID") or "")

    status = 500
    error = None
    # use_span 把 span 设为"当前"，这样 tracing.inject() 才知道该回写哪条 trace
    with _otel_trace.use_span(span, end_on_exit=False):
        try:
            response = await call_next(request)
            status = response.status_code
            span.set_attribute("http.response.status_code", status)
            response.headers["traceparent"] = tracing.inject({}).get("traceparent", "")
            return response
        except Exception as exc:                   # noqa: BLE001 - 记录后原样抛给外层
            error = exc
            raise
        finally:
            tracing.finish(span, status_code=status, error=error)
            span.end()


async def _rate_limit(request: Request, call_next: Any) -> Response:
    """令牌桶限流。放在 `_observe` **里面**：这样 429 也会被计入 HTTP 指标和 trace_id。"""
    path = request.url.path
    if not ratelimit.enabled() or path in ratelimit.SKIP_PATHS:
        return await call_next(request)

    limiter = ratelimit.get_limiter()
    key = ratelimit.bucket_key(request)
    allowed, retry_after = limiter.check(key)
    if allowed:
        return await call_next(request)

    label = ratelimit.route_label(path)
    metrics.RATE_LIMITED.labels(route=label).inc()
    wait = max(1, int(retry_after + 0.999))          # 向上取整：宁可多等 1 秒也别打空
    logger.warning("限流命中 path=%s bucket=%s retry_after=%ss", label, key, wait)
    return JSONResponse(
        {"error": "rate_limited",
         "detail": {"limit": limiter.limit, "window_seconds": limiter.window,
                    "retry_after": wait,
                    "hint": "请按 Retry-After 退避；不要原速率重试"},
         "trace_id": current_trace_id()},
        status_code=429,
        headers={"Retry-After": str(wait),
                 "X-RateLimit-Limit": str(limiter.limit),
                 "X-RateLimit-Remaining": "0"},
    )


# ── 优雅停机 ─────────────────────────────────────────────────
# 收到 SIGTERM 时不能立刻断：K8s / 滚动发布会给 30 秒宽限期，
# 这时如果直接退出，in-flight 请求会 502，worker 处理一半的消息会重复消费。
# 做法：先把 /readyz 变成 503（让负载均衡不再派新请求），再等 in-flight 归零。
_DRAINING = {"on": False}
_INFLIGHT = {"n": 0}
_DRAIN_TIMEOUT_SEC = float(os.environ.get("TDP_DRAIN_TIMEOUT", "20"))


def begin_drain() -> None:
    _DRAINING["on"] = True


def is_draining() -> bool:
    return _DRAINING["on"]


def inflight() -> int:
    return _INFLIGHT["n"]


def reset_drain_state() -> None:
    """测试用：把状态复位（全局变量，不 reset 会污染下一个用例）。"""
    _DRAINING["on"] = False
    _INFLIGHT["n"] = 0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 每次启动都复位：模块级状态会被同一进程里的上一个 app 实例污染（测试里尤其明显）
    reset_drain_state()
    logger.info("服务启动 version=%s log_format=%s", VERSION,
                (os.environ.get("TDP_LOG_FORMAT") or "text").lower())
    yield
    # 关停：先摘流量，再等在途请求跑完
    begin_drain()
    waited = 0.0
    while _INFLIGHT["n"] > 0 and waited < _DRAIN_TIMEOUT_SEC:
        await asyncio.sleep(0.1)
        waited += 0.1
    if waited:
        logger.info("优雅停机：等待在途请求 %.1fs，剩余 %s 个", waited, _INFLIGHT["n"])
    else:
        logger.info("优雅停机：没有在途请求")


def create_app() -> FastAPI:
    logs.setup()                         # TDP_LOG_FORMAT=json|text（默认 text）

    tracing.setup()                      # 幂等；没配 OTLP 端点就只在进程内用 span
    app = FastAPI(
        lifespan=_lifespan,
        title="测试数据管理平台",
        version=VERSION,
        description=(
            "整车测试数据管理后端：车辆档期占用 / 执行任务领取 / 问题单状态机 / 日报汇总与对账。\n\n"
            "**写接口必须带 `Idempotency-Key`**：同 key 同 body 返回首次响应，同 key 换 body 返回 422。\n"
            "错误体统一为 `{error, detail, trace_id}`。"
        ),
    )

    install_error_handlers(app)
    # 中间件顺序：先 add 的在**内层**。所以实际链路是
    #   tracing(最外) → trace_id/指标 → 限流 → 路由
    # 限流在最内层的业务之前，但 trace_id 和指标已经生效（429 也能被观测到）。
    app.middleware("http")(_rate_limit)
    app.middleware("http")(_observe)
    app.middleware("http")(_trace)          # 最后 add = 最外层

    # 认证：只有 /v1/auth/token 是公开的（否则没法冷启动换 token）。
    # 其余按"路由器"粗粒度授权 —— 一个路由器的读写通常是同一类人，
    # 细分到单条路由的权限（比如"只读用户能看问题单但不能提"）属于下一步的 ABAC。
    app.include_router(auth_router)
    app.include_router(meta_router, dependencies=[Depends(require_read_write())])
    app.include_router(catalog_router, dependencies=[Depends(require_read_write())])
    app.include_router(bookings_router, dependencies=[Depends(require_read_write())])
    app.include_router(defects_router, dependencies=[Depends(require_read_write())])
    app.include_router(queue_router, dependencies=[Depends(require_read_write())])
    app.include_router(executions_router, dependencies=[Depends(require_read_write())])
    # 汇总 / 对账 / 死信重放会影响全库数据，只给管理员
    app.include_router(jobs_router, dependencies=[Depends(require_roles("admin"))])
    app.include_router(admin_router, dependencies=[Depends(require_roles("admin"))])

    # 前端构建产物（web/dist）：有就挂到 /ui，没有就只提供 API。
    # 同源部署，所以不需要给浏览器开 CORS。
    web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if web_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=str(web_dist), html=True), name="ui")

    @app.get("/healthz", tags=["ops"], summary="存活探针（不查库）")
    def healthz() -> Dict[str, str]:
        # liveness 只回答"进程还在不在"。查库会带来误杀：DB 抖动一下就把容器重启了，
        # 重启并不能修好 DB，反而让故障扩大。
        return {"status": "ok", "service": SERVICE}

    @app.get("/readyz", tags=["ops"], summary="就绪探针（查库）")
    def readyz() -> Response:
        if is_draining():
            # 停机信号已收到：告诉负载均衡别再派活，但已有请求继续跑完
            return JSONResponse({"status": "draining", "db": "unknown",
                                 "trace_id": current_trace_id()}, status_code=503)
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
            "ui": "/ui/",
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
