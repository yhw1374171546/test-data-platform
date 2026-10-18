"""Prometheus 指标：HTTP 维度 + 业务维度。

业务维度才是这个项目值得看的部分——`tdp_booking_attempts_total{result}` 的分布
就是"1000 并发抢同一档期"实验的线上版本：
`result="created"` 应该永远只有 1，`result="slot_taken"` 是其余全部。
如果哪天 created 变成 2，说明唯一约束/条件更新防线破了，看板会立刻报警。

prometheus_client 做成可选依赖：单元测试、CI 精简镜像里不装也能起服务，
此时所有指标退化成空对象（打点代码不需要写 if）。
"""
from __future__ import annotations

from typing import Any

try:                                                    # pragma: no cover - 依赖环境
    from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge,
                                   Histogram, generate_latest)
    ENABLED = True
except ImportError:                                     # pragma: no cover
    ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"


class _Noop:
    """指标占位：`.labels(...)` 返回自己，`.inc()/.observe()/.set()` 什么也不做。"""

    def labels(self, *_: Any, **__: Any) -> _Noop:
        return self

    def inc(self, *_a: Any, **_k: Any) -> None:
        return None

    def observe(self, *_a: Any, **_k: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None


def _counter(name: str, doc: str, labels: tuple = ()) -> Any:
    return Counter(name, doc, labels) if ENABLED else _Noop()


def _histogram(name: str, doc: str, labels: tuple = ()) -> Any:
    return Histogram(name, doc, labels) if ENABLED else _Noop()


def _gauge(name: str, doc: str, labels: tuple = ()) -> Any:
    return Gauge(name, doc, labels) if ENABLED else _Noop()


# ── HTTP 维度 ───────────────────────────────────────────────
# path 用路由模板（/v1/bookings/{booking_no}/cancel）而不是真实 URL，
# 否则每个 id 都会生成一条时间序列，直接把 Prometheus 打爆。
HTTP_REQUESTS = _counter("tdp_http_requests_total", "HTTP 请求数",
                         ("method", "path", "status"))
HTTP_LATENCY = _histogram("tdp_http_request_duration_seconds", "HTTP 请求耗时",
                          ("method", "path"))
HTTP_INFLIGHT = _gauge("tdp_http_inflight", "当前处理中的请求数")

# ── 业务维度 ───────────────────────────────────────────────
BOOKINGS = _counter("tdp_booking_attempts_total", "预约档期结果分布",
                    ("result",))            # created|replayed|slot_taken|body_mismatch
DEFECT_TRANSITIONS = _counter("tdp_defect_transitions_total", "问题单状态转移结果分布",
                              ("to_status", "result"))   # moved|already|body_mismatch
CLAIMS = _counter("tdp_task_claim_attempts_total", "任务领取结果分布",
                  ("result",))              # claimed|already_claimed|version_conflict|replayed
EXECUTIONS = _counter("tdp_execution_submits_total", "提交执行结果分布",
                      ("result", "code"))   # result=pass|fail|blocked|skip, code=recorded|replayed
WORKER_BATCHES = _counter("tdp_outbox_batches_total", "outbox worker 批次数",
                          ("outcome",))     # done|partial
SUMMARY_UPSERTS = _counter("tdp_summary_upserts_total", "日报汇总 upsert 次数")

# ── 积压类指标（能提前发现坏事的那些）────────────────────────
# pending 持续上涨 = worker 跟不上或挂了；dead > 0 = 必须告警 + 人工重放
OUTBOX_PENDING = _gauge("tdp_outbox_pending", "待处理的 outbox 消息数")
OUTBOX_DEAD = _gauge("tdp_outbox_dead", "死信数（需告警 + 人工重放）")
# 对账差异：按 kind 分开看，才能区分"少算"和"漏提单"
RECON_DISCREPANCIES = _gauge("tdp_recon_discrepancies", "最近一次对账的差异数",
                             ("kind",))

# ── 缓存（技术方案 4.3）──────────────────────────────────────
# 命中率掉下去 = 缓存没起作用或 TTL 太短；失效次数为 0 = 写路径忘了失效（很危险）
CACHE_HITS = _counter("tdp_cache_hits_total", "缓存命中次数", ("entity",))
CACHE_MISSES = _counter("tdp_cache_misses_total", "缓存未命中次数", ("entity",))
CACHE_INVALIDATIONS = _counter("tdp_cache_invalidations_total", "缓存失效次数",
                               ("reason",))

EXPORTER_ENABLED = ENABLED


def render() -> tuple:
    """返回 (响应体 bytes, content_type)；未安装 prometheus_client 时抛 RuntimeError。"""
    if not ENABLED:                                     # pragma: no cover
        raise RuntimeError("未安装 prometheus_client，无法导出指标")
    return generate_latest(), CONTENT_TYPE_LATEST
