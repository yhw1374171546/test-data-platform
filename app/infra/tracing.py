"""链路追踪：OpenTelemetry SDK + W3C `traceparent`。

为什么用 W3C 的 `traceparent` 而不是自造一个 header：
网关、Service Mesh、前端 RUM 都认这个格式；自造的 `X-Trace-Id` 到头来还是要在边界
做一次转换，不如一开始就用标准（这个项目里 `X-Request-ID` 仍然保留，
它是**业务可读**的请求号，和 trace 是两件事：一个给人看，一个给追踪系统拼）。

三个决定：

1. **默认不导出**：没配 `TDP_OTLP_ENDPOINT` 时只在本进程里生成 span，不产生任何网络请求。
   本地开发和 CI 不该因为"没起 collector"而变慢或报错。
2. **采样用 ParentBased(AlwaysOn)**：上游传了 `traceparent` 就跟着上游的决定（父采样），
   否则全采。生产要按比例采样时，改一行 sampler 即可，不用动业务。
3. **不引 `opentelemetry-instrumentation-fastapi`**：那套自动埋点会把 Starlette 中间件
   包成自己的 span，和本项目的 `trace_id`/指标中间件打架；手写 30 行反而更可控。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor, ConsoleSpanExporter,
                                            SimpleSpanProcessor)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger("tdp.trace")

PROPAGATOR = TraceContextTextMapPropagator()
TRACER_NAME = "tdp"
_provider: Optional[TracerProvider] = None


def _exporter_from_env() -> Any:
    """按环境变量决定导出目标；返回 None 表示不导出。"""
    endpoint = (os.environ.get("TDP_OTLP_ENDPOINT") or "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        # gRPC 端点：collector 默认 4317；insecure 是因为内网 collector 不走 TLS
        return OTLPSpanExporter(endpoint=endpoint, insecure=True)
    if (os.environ.get("TDP_TRACE_CONSOLE") or "").lower() in ("1", "on", "true"):
        return ConsoleSpanExporter()
    return None


def setup(service: str = "test-data-platform") -> TracerProvider:
    """初始化全局 TracerProvider（幂等）。默认不挂 exporter。"""
    global _provider
    if _provider is not None:
        return _provider
    provider = TracerProvider(
        resource=Resource.create({"service.name": service}),
        sampler=ParentBased(ALWAYS_ON),
    )
    exporter = _exporter_from_env()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("链路追踪已启用，导出到 %s", type(exporter).__name__)
    _provider = provider
    trace.set_tracer_provider(provider)
    return provider


def provider() -> TracerProvider:
    return setup()


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def attach_exporter(exporter: Any) -> None:
    """给测试/诊断挂一个 exporter（例如 InMemorySpanExporter）。

    注意：全局 provider 只能 set 一次，但**span processor 可以后加**，
    所以测试不需要在导入 app 之前做准备。
    """
    provider().add_span_processor(SimpleSpanProcessor(exporter))


def inject(headers: Dict[str, str]) -> Dict[str, str]:
    """把当前上下文写进 headers（响应回写 traceparent 用）。"""
    PROPAGATOR.inject(headers)
    return headers


def extract(headers: Any) -> Any:
    """从请求头里取出上游上下文；没有就返回空上下文（等于新开一条 trace）。"""
    return PROPAGATOR.extract(carrier={k.lower(): v for k, v in dict(headers).items()})


def record_exception(exc: BaseException) -> None:
    """把异常记到**当前** span 上（含堆栈）。

    为什么不在最外层 trace 中间件里记：本项目的 `_observe` 中间件会兜底捕获异常、
    返回统一形状的 500 JSON（刻意的，不能让 Starlette 的纯文本 500 漏给前端），
    所以异常到达最外层时已经变成"一个 500 响应"了。真正的捕获点在 `_observe`。
    """
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


def finish(span: trace.Span, *, status_code: int, error: Optional[BaseException] = None) -> None:
    """统一收尾：状态码 ≥500 记 ERROR，异常额外 record。

    注意"不降级"：内层已经把 span 标成 ERROR（比如 `record_exception`）时，
    这里不能再用 OK 覆盖 —— 否则追踪系统里就看不到真正的失败原因。
    """
    already_error = span.status.status_code is StatusCode.ERROR
    if error is not None:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
    elif status_code >= 500:
        if not already_error:
            span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))
    elif not already_error:
        span.set_status(Status(StatusCode.OK))


__all__ = ["SpanKind", "PROPAGATOR", "attach_exporter", "extract", "finish",
           "inject", "provider", "setup", "tracer"]
