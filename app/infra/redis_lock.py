"""Redis 预占：技术方案 §4.1 的 C 方案（"抢购式"峰值场景）。

**它不替代数据库的唯一约束，只是挡在数据库前面的一层漏斗。**

    N 个请求抢同一个档期
      → 先问 Redis：这个 slot key 能不能 SETNX 成功？
         · 失败（99%）→ 直接返回"已被占"，一次 DB 都不打
         · 成功（1%）→ 继续走数据库的条件 UPDATE（真相仍由 DB 定）

为什么还要走 DB：Redis 里的预占是"意图"，不是"事实"。
预占成功但 DB 写失败（事务回滚、连接断）必须把 key 放掉，否则档期会被"幽灵占位"卡住 TTL。
所以这里的顺序是 **先占 Redis → 再写 DB → DB 失败则释放 Redis**。

代价（写进报告里，别藏着）：
  · 预占 key 有 TTL（默认 5s）。如果这段时间里预约被取消了，新的预约会被 Redis 挡住，
    直到 TTL 过期 —— 这是"用一点点正确性换巨大的吞吐"，只在抢购式场景值得。
  · 多副本部署时所有副本共享同一个 Redis，所以预占是全局有效的（这是它比进程内锁强的地方）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from app.infra import cache

logger = logging.getLogger("tdp.slotlock")

RESERVE_TTL = int(os.environ.get("TDP_SLOT_RESERVE_TTL", "5"))
KEY_PREFIX = "tdp:slot"

# SET key holder NX EX ttl —— 一条命令完成"不存在才写 + 带过期"，
# 不需要 GET+SET 两步（那两步之间就是并发窗口）。
_RESERVE_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
    return 1
end
return 0
"""


def slot_key(vehicle_id: int, slot_date: str, half: int) -> str:
    return f"{KEY_PREFIX}:{vehicle_id}:{slot_date}:{half}"


def reserve(*, vehicle_id: int, slot_date: str, half: int, holder: str,
            ttl: int = 0) -> Optional[bool]:
    """尝试预占。

    返回 True  = 抢到预占，可以往下走 DB；
    返回 False = 已被别人预占（快速失败，不打 DB）；
    返回 None  = Redis 不可用（**降级**：不做预占，直接靠 DB 的条件更新兜底）。
    """
    client = cache.client()
    if client is None:
        return None
    key = slot_key(vehicle_id, slot_date, half)
    try:
        ok = client.eval(_RESERVE_LUA, 1, key, holder, int(ttl or RESERVE_TTL))
        return bool(ok)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Redis 预占失败，降级为直连 DB：%s", exc)
        return None


def release(*, vehicle_id: int, slot_date: str, half: int, holder: str) -> bool:
    """释放预占。**只释放自己占的那个**（比对 holder），避免把别人的预占删掉。"""
    client = cache.client()
    if client is None:
        return False
    key = slot_key(vehicle_id, slot_date, half)
    try:
        current = client.get(key)
        if current == holder:
            client.delete(key)
            return True
        return False
    except Exception as exc:                       # noqa: BLE001
        logger.warning("释放预占失败：%s", exc)
        return False


def force_release(*, vehicle_id: int, slot_date: str, half: int) -> bool:
    """不看 holder 直接删预占。

    用在"取消预约"上：取消是**数据库层的确定事实**，此时 Redis 里残留的预占
    只会挡住后续合法预约（直到 TTL），所以要直接清掉 ——
    否则会出现"取消了却约不回来"这种最莫名其妙的 bug。
    """
    client = cache.client()
    if client is None:
        return False
    try:
        client.delete(slot_key(vehicle_id, slot_date, half))
        return True
    except Exception as exc:                       # noqa: BLE001
        logger.warning("强制释放预占失败：%s", exc)
        return False


def release_all_for_vehicle(vehicle_id: int) -> int:
    """清掉某台车的所有预占（运维/实验收尾用）。"""
    client = cache.client()
    if client is None:
        return 0
    removed = 0
    try:
        for key in client.scan_iter(match=f"{KEY_PREFIX}:{vehicle_id}:*", count=200):
            client.delete(key)
            removed += 1
    except Exception as exc:                       # noqa: BLE001
        logger.warning("清理预占失败：%s", exc)
    return removed
