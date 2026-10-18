"""Redis 缓存：**三种失效策略 + 一个负对照开关**（技术方案 §4.3）。

策略（`TDP_CACHE_STRATEGY`，运行时也可以 `set_strategy()` 切，实验要用）：

| 策略 | 做法 | 脏读窗口 |
| --- | --- | --- |
| `off` | 不用缓存，直接读库 | 0（但没有缓存收益） |
| `none` | **有缓存但从不失效** | 永久脏 —— 这是**负对照**，用来证明实验真的能测出脏读 |
| `delete` | A：事务提交**后**删 key（Cache-Aside） | 并发下"读旧值回填"仍会脏 |
| `double_delete` | B：写前删一次 + 提交后**延迟**再删一次 | 窗口变小，取决于延迟是否大于读耗时 |
| `outbox` | C：写事务里插消息，worker 消费后删 | 删除**不会丢**（消息重试），窗口由消费延迟决定 |

为什么必须有 `none` 这个"故意做错"的策略：
如果一上来只跑 A/B/C 且结果都是"脏读 0"，你无法区分"方案真的没问题"和
"实验根本没打中窗口"。**先证明实验能测出脏读，再说自己没脏读**，这是实验设计的基本功。

一个诚实的前提（报告里也会写）：三种失效策略**都不能证明窗口为 0**。
它们做的是"把窗口压到比读请求耗时还短"，真正的兜底是 TTL ——
所以 `set_json` 永远带 TTL，且 `delete()` 失败不会静默（见 outbox 消费者）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from app.infra import metrics

logger = logging.getLogger("tdp.cache")

try:                                                    # 依赖可选：没装 redis 也能跑（降级为不缓存）
    import redis as _redis
except ImportError:                                     # pragma: no cover
    _redis = None

REDIS_URL = os.environ.get("TDP_REDIS_URL", "redis://127.0.0.1:6380/0")
DEFAULT_TTL = int(os.environ.get("TDP_CACHE_TTL", "300"))
DOUBLE_DELETE_DELAY = float(os.environ.get("TDP_CACHE_DOUBLE_DELAY", "0.05"))
# redis-py 8 的连接池默认上限是 **100**，高并发压测时会直接报 "Too many connections"
# （报错长得像服务端拒绝，其实是客户端池子满了）。这里显式放宽。
MAX_CONNECTIONS = int(os.environ.get("TDP_REDIS_MAX_CONNECTIONS", "256"))

KEY_PREFIX = "tdp"
TOPIC_CACHE_INVALIDATE = "cache_invalidate"

STRATEGIES = ("off", "none", "delete", "double_delete", "outbox")
DEFAULT_STRATEGY = os.environ.get("TDP_CACHE_STRATEGY", "delete").strip().lower()

_strategy = DEFAULT_STRATEGY if DEFAULT_STRATEGY in STRATEGIES else "delete"

_lock = threading.Lock()
_client: Any = None
_dead_until = 0.0

_hits = 0
_misses = 0


# ────────────────────────── 策略 ──────────────────────────
def strategy() -> str:
    return _strategy


def set_strategy(name: str) -> str:
    """切策略（实验用）。返回生效的策略名。"""
    global _strategy
    value = str(name or "").strip().lower()
    if value not in STRATEGIES:
        raise ValueError(f"未知缓存策略：{name!r}；可用：{', '.join(STRATEGIES)}")
    _strategy = value
    return _strategy


def caching_enabled() -> bool:
    return _strategy != "off"


# ────────────────────────── 连接 ──────────────────────────
def _conn() -> Any:
    global _client, _dead_until
    if _redis is None:
        return None
    if _client is not None:
        return _client

    now = time.monotonic()
    if now < _dead_until:                 # 刚失败过，先别每读一次都重连（会把 P99 拖爆）
        return None

    with _lock:
        if _client is not None:
            return _client
        try:
            # protocol=2：redis-py 8 默认用 RESP3 握手（HELLO 3），而本机绿色版 Redis 是 5.0，
            # 不认 HELLO。显式用 RESP2 就兼容了 —— 踩过一次，报 "unknown command HELLO"。
            client = _redis.Redis.from_url(
                REDIS_URL, decode_responses=True, protocol=2,
                max_connections=MAX_CONNECTIONS,
                socket_connect_timeout=0.5, socket_timeout=1.0)
            client.ping()
            _client = client
            logger.info("Redis 已连接：%s", REDIS_URL)
            return client
        except Exception as exc:          # noqa: BLE001
            _dead_until = now + 5.0
            logger.warning("Redis 不可用，降级为直读 DB：%s", exc)
            return None


def available() -> bool:
    return _conn() is not None


def client() -> Any:
    """拿底层 Redis 客户端（可用性由调用方自己判断，返回 None 表示不可用）。

    给 `infra/redis_lock.py` 这类同进程的组件复用连接池用 ——
    每个模块各建一个客户端池，高并发下连接数会翻倍。
    """
    return _conn()


def reset_connection() -> None:
    """测试/故障切换用：丢掉缓存的连接，下次重新连。"""
    global _client, _dead_until
    _client = None
    _dead_until = 0.0


# ────────────────────────── key ──────────────────────────
def key_vehicle(vin: str) -> str:
    return f"{KEY_PREFIX}:vehicle:{vin}"


def key_case(case_no: str) -> str:
    return f"{KEY_PREFIX}:case:{case_no}"


# ────────────────────────── 读写 ──────────────────────────
def get_json(key: str, *, entity: str = "vehicle") -> Optional[Dict[str, Any]]:
    global _hits, _misses
    if not caching_enabled():
        return None
    client = _conn()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:              # noqa: BLE001
        logger.warning("缓存读失败，降级直读 DB：%s", exc)
        return None

    if raw is None:
        with _lock:
            _misses += 1
        metrics.CACHE_MISSES.labels(entity=entity).inc()
        return None

    with _lock:
        _hits += 1
    metrics.CACHE_HITS.labels(entity=entity).inc()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:          # 脏数据当作没命中
        return None


def set_json(key: str, value: Dict[str, Any], *, ttl: Optional[int] = None) -> bool:
    if not caching_enabled():
        return False
    client = _conn()
    if client is None:
        return False
    try:
        client.set(key, json.dumps(value, ensure_ascii=False, default=str),
                   ex=int(ttl or DEFAULT_TTL))
        return True
    except Exception as exc:              # noqa: BLE001
        logger.warning("缓存写失败：%s", exc)
        return False


def delete(*keys: str) -> bool:
    """删 key。返回 False 表示**没删成**（缓存不可用）——调用方要据此决定是否重试。"""
    keys = tuple(k for k in keys if k)
    if not keys:
        return True
    client = _conn()
    if client is None:
        return False
    try:
        client.delete(*keys)
        return True
    except Exception as exc:              # noqa: BLE001
        logger.warning("缓存删除失败：%s", exc)
        return False


def delete_prefix(prefix: str) -> int:
    """按前缀清 key（实验收尾/运维用）。返回删除条数。"""
    client = _conn()
    if client is None:
        return 0
    removed = 0
    try:
        for key in client.scan_iter(match=f"{prefix}*", count=500):
            client.delete(key)
            removed += 1
    except Exception as exc:              # noqa: BLE001
        logger.warning("按前缀清缓存失败：%s", exc)
    return removed


def set_ttl(*keys: str, ttl: int = 1) -> None:
    """给 key 设短 TTL：实验里用来模拟"TTL 兜底"（真实系统的 TTL 是分钟级）。"""
    client = _conn()
    if client is None:
        return
    for key in keys:
        try:
            client.expire(key, int(ttl))
        except Exception:                 # noqa: BLE001
            return


def schedule_delayed_delete(key: str, delay: Optional[float] = None) -> None:
    """延迟双删的"第二次删除"。

    真实系统里这个延迟通常由**消息延迟**或定时任务提供；这里用守护线程定时器模拟，
    并在注释里说清楚：**延迟是一个猜出来的数**，它必须大于读请求的 DB 耗时才有意义。
    """
    # 注意 args 要写成 (key,)：Timer 会用 delete(*args) 调用，
    # 写成 ((key,),) 就会变成 delete((key,))，redis-py 会报 "Invalid input of type: 'tuple'"。
    timer = threading.Timer(float(delay if delay is not None else DOUBLE_DELETE_DELAY),
                            delete, args=(key,))
    timer.daemon = True
    timer.start()


# ────────────────────────── 统计 ──────────────────────────
def stats() -> Dict[str, Any]:
    with _lock:
        hits, misses = _hits, _misses
    total = hits + misses
    return {
        "strategy": _strategy,
        "hits": hits,
        "misses": misses,
        "total": total,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "available": available(),
        "ttl": DEFAULT_TTL,
    }


def reset_stats() -> None:
    global _hits, _misses
    with _lock:
        _hits = 0
        _misses = 0


def clear_all(prefix: str = f"{KEY_PREFIX}:") -> int:
    return delete_prefix(prefix)


__all__: List[str] = ["DEFAULT_TTL", "KEY_PREFIX", "REDIS_URL", "STRATEGIES",
                      "TOPIC_CACHE_INVALIDATE", "available", "caching_enabled",
                      "clear_all", "delete", "delete_prefix", "get_json",
                      "key_case", "key_vehicle", "reset_connection",
                      "reset_stats", "schedule_delayed_delete", "set_json",
                      "set_strategy", "set_ttl", "stats", "strategy"]
