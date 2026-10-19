"""令牌桶限流。

为什么是令牌桶、不是"窗口计数"：
窗口计数在窗口边界会放过 2 倍流量（前一秒末尾 + 后一秒开头各一窗），
令牌桶按速率匀速补充，边界上没有突变；而且桶容量天然就是"允许的突发量"。

三个刻意的决定：

1. **按"是谁"分桶，不是全局限流**：一个客户端把配额打满，不该拖累别人。
   有 token 就按用户分，没有就按来源 IP 分（未认证请求也要限，否则扫口令不花钱）。
2. **进程内实现，单实例有效**：多副本要换成 Redis（INCR + EXPIRE 或 Lua）。
   这里不假装支持多副本 —— 文档和 README 都写清楚，比"看起来分布式、实际各自限各的"强。
3. **拒绝要带 `Retry-After`**：不给的话客户端只能瞎重试，反而放大流量。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

DEFAULT_LIMIT = 600          # 每个窗口允许的请求数
DEFAULT_WINDOW = 60.0        # 窗口秒数


def enabled() -> bool:
    return (os.environ.get("TDP_RATE_LIMIT_ENABLED", "on") or "").strip().lower() \
        not in ("off", "0", "false", "no")


def limit() -> int:
    try:
        return max(1, int(os.environ.get("TDP_RATE_LIMIT", str(DEFAULT_LIMIT))))
    except ValueError:
        return DEFAULT_LIMIT


def window() -> float:
    try:
        return max(0.1, float(os.environ.get("TDP_RATE_WINDOW", str(DEFAULT_WINDOW))))
    except ValueError:
        return DEFAULT_WINDOW


class TokenBucket:
    """单个桶：容量 = 允许的突发量，rate = 每秒补充的令牌数。"""

    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, capacity: float, rate: float, *, now: Optional[float] = None):
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.tokens = float(capacity)
        self.updated = time.monotonic() if now is None else float(now)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now

    def take(self, tokens: float = 1.0, *, now: Optional[float] = None) -> Tuple[bool, float]:
        """取令牌。返回 (是否放行, 建议重试秒数)。"""
        moment = time.monotonic() if now is None else float(now)
        self._refill(moment)
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True, 0.0
        missing = tokens - self.tokens
        return False, missing / self.rate if self.rate else float("inf")

    def remaining(self) -> float:
        return max(0.0, self.tokens)


class Limiter:
    def __init__(self, *, limit: int, window_seconds: float):
        self.limit = int(limit)
        self.window = float(window_seconds)
        self.rate = self.limit / self.window
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    @property
    def capacity(self) -> float:
        return float(self.limit)

    def check(self, key: str, *, now: Optional[float] = None) -> Tuple[bool, float]:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.rate, now=now)
                self._buckets[key] = bucket
            return bucket.take(now=now)

    def remaining(self, key: str) -> float:
        with self._lock:
            bucket = self._buckets.get(key)
            return bucket.remaining() if bucket else self.capacity

    def reset(self) -> None:
        """清空所有桶（测试和"改了配额立刻生效"用）。"""
        with self._lock:
            self._buckets.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"buckets": len(self._buckets), "limit": self.limit,
                    "window": self.window, "rate": self.rate}


_CACHED: Dict[str, Any] = {"key": None, "limiter": None}
_CACHE_LOCK = threading.Lock()


def get_limiter() -> Limiter:
    """按当前配置取单例；配置变了（测试 monkeypatch、线上改配额）就重建。"""
    signature = (limit(), window())
    with _CACHE_LOCK:
        if _CACHED["key"] != signature:
            _CACHED["limiter"] = Limiter(limit=signature[0], window_seconds=signature[1])
            _CACHED["key"] = signature
        return _CACHED["limiter"]


def reset() -> None:
    with _CACHE_LOCK:
        _CACHED["key"] = None
        _CACHED["limiter"] = None


SKIP_PATHS = frozenset({"/", "/healthz", "/readyz", "/metrics", "/docs", "/redoc",
                        "/openapi.json", "/favicon.ico"})
"""探针和指标不能被限流：K8s 每 10s 探一次活，限流把它挡掉就是自己把自己判死。"""


def bucket_key(request: Any) -> str:
    """有 token 按用户分桶，没有按 IP。

    这里**只解 token 不验签**：限流的目的是分桶，不是鉴权 ——
    非法 token 会在后面的鉴权环节被 401，但如果在这里因为它"解析失败"就退化成
    共享 IP 桶，反而给了伪造 token 刷别人配额的机会。
    """
    from app.infra import auth

    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        try:
            payload = auth.decode_token(header[7:])
            return f"user:{payload.get('sub')}"
        except Exception:                              # noqa: BLE001 - 解不开就按 IP
            pass
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) or "unknown"
    return f"ip:{host}"


def route_label(path: str) -> str:
    """指标标签用前两段（`/v1/bookings`），避免每个 id 一条时间序列。"""
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts[:2]) if parts else "/"
