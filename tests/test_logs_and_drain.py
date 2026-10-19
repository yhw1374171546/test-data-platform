"""结构化日志 + 优雅停机的测试。

这两个都是"平时看不出、出事才要命"的环节，所以测的不是"能跑"，
而是**字段对不对**和**状态会不会串**。
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api.context import reset_trace_id, set_trace_id
from app.infra import logs
from app.main import begin_drain, inflight, is_draining, reset_drain_state


@pytest.fixture(autouse=True)
def _clean_drain_state():
    reset_drain_state()
    yield
    reset_drain_state()


def test_json_log_has_trace_id_and_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    token = set_trace_id("trace-abc")
    try:
        assert logs.setup(level="INFO", fmt="json") == "json"
        logging.getLogger("tdp.test").info("打个日志", extra={"path": "/v1/queue", "status": 200})
    finally:
        reset_trace_id(token)

    line = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")][-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "打个日志"
    assert payload["trace_id"] == "trace-abc", "没有 trace_id 就没法按请求聚合"
    assert payload["path"] == "/v1/queue" and payload["status"] == 200


def test_text_format_is_still_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert logs.setup(level="INFO", fmt="text") == "text"
    logging.getLogger("tdp.test").info("纯文本")
    out = capsys.readouterr().err
    assert "纯文本" in out and not out.strip().startswith("{")


def test_setup_is_idempotent() -> None:
    logs.setup(fmt="json")
    logs.setup(fmt="json")
    assert len(logging.getLogger().handlers) == 1, "重复 setup 不能叠加 handler"

    logs.setup(fmt="text")                     # 还原，避免影响其它用例


def test_readyz_returns_503_when_draining(client: TestClient) -> None:
    assert client.get("/readyz").status_code == 200

    begin_drain()
    assert is_draining() is True
    resp = client.get("/readyz")
    assert resp.status_code == 503, "停机中必须让负载均衡摘流量"
    assert resp.json()["status"] == "draining"

    reset_drain_state()
    assert client.get("/readyz").status_code == 200


def test_inflight_counter_returns_to_zero(client: TestClient) -> None:
    for _ in range(3):
        client.get("/healthz")
    assert inflight() == 0, "请求结束后计数必须归零，否则优雅停机会白等 20 秒"
