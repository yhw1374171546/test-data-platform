"""限流测试：令牌桶算术 + HTTP 语义（429 / Retry-After / 白名单 / 分桶隔离）。

限流最容易踩的两个坑：
1. 把探针也限了 —— K8s 探活被 429，容器被判死，故障自己造；
2. 全局限流 —— 一个客户端打满配额，所有客户端一起躺。
下面各有一条用例钉住。
"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from app.infra import ratelimit


@pytest.fixture(autouse=True)
def _fresh_limiter():
    ratelimit.reset()
    yield
    ratelimit.reset()


# ────────────────────────── 令牌桶算术 ──────────────────────────
def test_bucket_allows_burst_then_denies() -> None:
    bucket = ratelimit.TokenBucket(capacity=3, rate=1.0, now=0.0)
    allowed = [bucket.take(now=0.0)[0] for _ in range(4)]
    assert allowed == [True, True, True, False]


def test_bucket_refills_over_time() -> None:
    bucket = ratelimit.TokenBucket(capacity=2, rate=1.0, now=0.0)
    assert bucket.take(now=0.0)[0] is True
    assert bucket.take(now=0.0)[0] is True
    assert bucket.take(now=0.0)[0] is False
    assert bucket.take(now=1.0)[0] is True          # 过 1 秒补 1 个


def test_bucket_never_exceeds_capacity() -> None:
    bucket = ratelimit.TokenBucket(capacity=2, rate=10.0, now=0.0)
    bucket.take(now=100.0)                          # 补满但不超过 2
    assert bucket.remaining() <= 2.0


def test_retry_after_is_positive_and_finite() -> None:
    limiter = ratelimit.Limiter(limit=1, window_seconds=10)      # 0.1 个/秒
    limiter.check("k", now=0.0)
    allowed, retry_after = limiter.check("k", now=0.0)
    assert allowed is False
    assert 0 < retry_after <= 11


# ────────────────────────── HTTP 语义 ──────────────────────────
def test_returns_429_with_retry_after(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("TDP_RATE_LIMIT", "3")
    monkeypatch.setenv("TDP_RATE_WINDOW", "60")
    ratelimit.reset()

    codes = [client.get("/v1/defects").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429], codes

    resp = client.get("/v1/defects")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limited"
    assert body["detail"]["limit"] == 3
    assert body["trace_id"], "429 也必须带 trace_id，否则排查时找不到这次请求"
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_health_and_metrics_are_exempt(client: TestClient, monkeypatch) -> None:
    """探针和 /metrics 不能被限流：K8s 探活被挡掉就是自己把容器判死。"""
    monkeypatch.setenv("TDP_RATE_LIMIT", "1")
    monkeypatch.setenv("TDP_RATE_WINDOW", "60")
    ratelimit.reset()

    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_buckets_are_isolated_between_identities(client: TestClient,
                                                 tester_client: TestClient,
                                                 monkeypatch) -> None:
    """admin 把配额打满，不能影响 tester —— 否则一个客户端就能拖垮所有人。"""
    monkeypatch.setenv("TDP_RATE_LIMIT", "1")
    monkeypatch.setenv("TDP_RATE_WINDOW", "60")
    ratelimit.reset()

    assert client.get("/v1/defects").status_code == 200
    assert client.get("/v1/defects").status_code == 429
    assert tester_client.get("/v1/defects").status_code == 200


def test_disable_switch(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("TDP_RATE_LIMIT", "1")
    monkeypatch.setenv("TDP_RATE_LIMIT_ENABLED", "off")
    ratelimit.reset()
    assert ratelimit.enabled() is False
    assert [client.get("/v1/defects").status_code for _ in range(3)] == [200, 200, 200]


def test_metric_records_rejections(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("TDP_RATE_LIMIT", "1")
    ratelimit.reset()
    client.get("/v1/defects")
    client.get("/v1/defects")                       # 触发 429
    assert "tdp_rate_limited_total" in client.get("/metrics").text


def test_config_is_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TDP_RATE_LIMIT", "42")
    monkeypatch.setenv("TDP_RATE_WINDOW", "2")
    limiter = ratelimit.get_limiter()
    assert limiter.limit == 42 and limiter.window == 2.0
    assert limiter.rate == pytest.approx(21.0)


def test_route_label_has_no_ids() -> None:
    """指标标签不能用真实 URL：/v1/defects/123 会把基数打爆。"""
    assert ratelimit.route_label("/v1/defects/123") == "/v1/defects"
    assert ratelimit.route_label("/v1/vehicles/VIN001") == "/v1/vehicles"
