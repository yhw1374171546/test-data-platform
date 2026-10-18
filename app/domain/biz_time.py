"""业务日期口径：**TIMESTAMPTZ → 本地自然日 的唯一换算入口**（技术方案 §9 坑 #4）。

坑在哪：
`executions.executed_at` 是 TIMESTAMPTZ，而日报 `daily_exec_summary.biz_date` 按"自然日"切。
如果直接写 `executed_at::date`，PG 会按**会话时区**截断——本机恰好是 Asia/Shanghai 没问题，
但容器里默认常是 UTC，于是每天 00:00~08:00 的执行会被算到**前一天**，日报和后端明细每天差几条。

这种差异不会报错，只会让对账结果"每天都有噪声"，最后没人再信对账。
所以"这是哪一天"只能有一种算法，且写在一处：

  · 提交执行结果时用它算 `biz_date` 写进 outbox（app/service/execution.py）
  · 第 3 周对账时用 `biz_date_sql()` 生成的等价 SQL 聚合 executions
两侧共用同一份规则，才有可能对得上。

为什么不用 `datetime.now().date()`：那是**进程本地时区**，多副本部署时不同机器给出不同答案。
这里用显式时区（默认 `Asia/Shanghai`，可用环境变量 `TDP_TZ` 覆盖），与机器在哪无关。
"""
from __future__ import annotations

import logging
import os
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("tdp.time")

TZ_NAME = os.environ.get("TDP_TZ", "Asia/Shanghai")

# Windows 不自带 IANA 时区库，缺 tzdata 包时 ZoneInfo 会失败。
# 对"没有夏令时的常用时区"退化成固定偏移是安全的；其余情况宁可启动就报错，
# 也不要静默用 UTC 把业务日期算错一天（那种 bug 查起来极贵）。
_FALLBACK_OFFSET_HOURS = {
    "Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Singapore": 8,
    "Asia/Taipei": 8, "UTC": 0, "Etc/UTC": 0,
}


def _load_zone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:                                # noqa: BLE001 - 缺 tzdata
        hours = _FALLBACK_OFFSET_HOURS.get(name)
        if hours is None:
            raise RuntimeError(
                f"时区 {name!r} 不可用：Windows 上需要 `pip install tzdata`，"
                f"或把 TDP_TZ 改成 UTC / Asia/Shanghai"
            ) from None
        logger.warning("时区库不可用，%s 退化为固定 UTC%+d（安装 tzdata 可消除此警告）",
                       name, hours)
        return timezone(timedelta(hours=hours), name)


TZ = _load_zone(TZ_NAME)

__all__ = ["TZ", "TZ_NAME", "biz_date_sql", "biz_day_bounds", "ensure_aware",
           "local_now", "now", "to_biz_date"]


def now() -> datetime:
    """当前时刻（UTC，带时区）。写库统一用这个，不要用 `datetime.now()` 裸调。"""
    return datetime.now(timezone.utc)


def ensure_aware(moment: datetime) -> datetime:
    """把 naive 时间按 **UTC** 解释（防御性：API 传进来的 ISO 串可能没带时区）。

    为什么按 UTC 而不是按本地：PG 的 timestamptz 语义就是 UTC 存储，
    naive 值在这里几乎总是"客户端漏了 Z"，当 UTC 解释不会造成二次偏移。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def local_now() -> datetime:
    return now().astimezone(TZ)


def to_biz_date(moment: datetime) -> date_cls:
    """某一时刻属于**本地**的哪一天。所有"哪一天"的判断都必须经过这里。"""
    return ensure_aware(moment).astimezone(TZ).date()


def biz_day_bounds(day: date_cls) -> Tuple[datetime, datetime]:
    """一个业务日的 UTC 区间 `[start, end)`，用于按日聚合/对账。

    用"本地墙钟时间 + 1 天"再加时区换算，而不是直接 +24h：
    有夏令时的时区里，一天可能是 23 或 25 小时，直接加 24h 会错位。
    """
    start_local = datetime.combine(day, time.min, tzinfo=TZ)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def biz_date_sql(column: str = "executed_at") -> str:
    """与 `to_biz_date()` 等价的 SQL 表达式（第 3 周对账聚合明细时用）。

    注意方向：`timestamptz AT TIME ZONE 'zone'` 得到的是**本地墙上时间**（timestamp without tz），
    再 `::date` 才是本地自然日。写反了（先 ::date 再 AT TIME ZONE）就是 UTC 日，会差一整天。
    """
    return f"({column} AT TIME ZONE '{TZ_NAME}')::date"
