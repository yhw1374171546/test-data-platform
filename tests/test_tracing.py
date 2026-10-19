"""链路追踪测试：W3C traceparent 的生成与透传、span 属性、错误标记。

为什么这些断言重要：
追踪系统的价值全在"能不能串起来"。如果 `traceparent` 不往回写、或者上游传了不认，
那这条 trace 就是断的 —— 比没有追踪更糟（你以为能看到全链路，实际只看得到自己这一段）。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.infra import tracing
from app.main import create_app

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
INBOUND = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    """整个模块共用一个内存 exporter：全局 provider 只能设一次，processor 可以后加。"""
    span_exporter = InMemorySpanExporter()
    tracing.attach_exporter(span_exporter)
    return span_exporter


@pytest.fixture()
def spans(exporter: InMemorySpanExporter) -> Iterator[Callable[[], List[Any]]]:
    """返回一个**取当前 span 列表的函数**，而不是列表本身。

    第一版写成 `yield exporter.get_finished_spans()`，那是在 fixture setup 时就求值了，
    拿到的是空快照 —— 所有断言都"没采到 span"，其实是夹具写错了。
    """
    exporter.clear()
    yield lambda: list(exporter.get_finished_spans())
    exporter.clear()


# ────────────────────────── 生成与格式 ──────────────────────────
def test_response_carries_w3c_traceparent(client: TestClient, spans) -> None:
    resp = client.get("/v1/defects")
    assert resp.status_code == 200
    header = resp.headers.get("traceparent")
    assert header, "响应必须回写 traceparent，否则调用方无法把两次请求串起来"
    match = TRACEPARENT.match(header)
    assert match, f"traceparent 格式不对：{header}"
    # 只要求"已采样"那一位（0x01）置位；OTel 1.44 实测会给出 03，规范只定义了最低位\n    assert int(match.group(3), 16) & 0x01 == 1, "未采样的 trace 没有任何意义"


def test_inbound_traceparent_is_continued(client: TestClient) -> None:
    """带上游的 traceparent 请求，新 span 必须挂到**同一条 trace** 上。"""
    resp = client.get("/v1/defects", headers={"traceparent": INBOUND})
    header = resp.headers["traceparent"]
    trace_id = TRACEPARENT.match(header).group(1)
    assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736", "上游 trace 丢了，链路就断了"
    assert TRACEPARENT.match(header).group(2) != "00f067aa0ba902b7", "span_id 必须是新的"


def test_span_records_route_and_status(client: TestClient, spans) -> None:
    with_request_id = "trace-test-1"
    client.get("/v1/defects", headers={"X-Request-ID": with_request_id})
    finished = spans()
    assert finished, "没有采集到 span，检查 exporter 是否挂上"
    span = finished[-1]
    attrs = dict(span.attributes or {})
    assert attrs["http.request.method"] == "GET"
    assert attrs["url.path"] == "/v1/defects"
    assert attrs["http.response.status_code"] == 200
    assert attrs["tdp.request_id"] == with_request_id, "span 要能和日志里的 trace_id 对上"


def test_error_status_marks_span_error(client: TestClient, spans,
                                       monkeypatch) -> None:
    monkeypatch.setenv("TDP_RATE_LIMIT", "1")
    from app.infra import ratelimit
    ratelimit.reset()
    first = client.get("/v1/defects")
    second = client.get("/v1/defects")             # 第 2 次被限流 → 429
    assert first.status_code == 200 and second.status_code == 429

    span = spans()[-1]
    assert span.attributes["http.response.status_code"] == 429
    # 4xx 不算服务端故障：span 仍应为 OK（只有 5xx / 异常才 ERROR）
    assert span.status.status_code.name == "OK"
    ratelimit.reset()


def test_5xx_marks_span_error(refs: Dict[str, Any], spans) -> None:
    """造一个一定会抛的接口，确认 span 被标成 ERROR 且带异常事件。"""
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
    assert resp.status_code == 500

    errors = [s for s in spans() if s.name == "GET /boom"]
    assert errors, "500 的 span 没被采集到"
    span = errors[-1]
    assert span.status.status_code.name == "ERROR"
    assert span.status.description and "kaboom" in span.status.description
    assert any("exception" in (event.name or "") for event in span.events), \
        "异常要作为 span event 记录下来，追踪系统才能展示堆栈"


def test_healthz_also_traced(client: TestClient, spans) -> None:
    client.get("/healthz")
    assert any(s.name == "GET /healthz" for s in spans())
