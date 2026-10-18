"""半天粒度的档期策略：技术方案 §1.3 那个业务决策的落点。

为什么单独抽一个文件：**"档期以半天为粒度"是业务决策，不是实现细节**。
把它写成显式常量 + 校验函数，任何人（包括面试官看代码）都能一眼看到
"这个系统只认 0/1 两个半天，不做分钟级区间"——而不是埋在 SQL 的 CHECK 里。
"""
from __future__ import annotations

from datetime import date as date_cls
from typing import Tuple

# 0=上午 1=下午。与 vehicle_slots.half 的 CHECK (half IN (0,1)) 一一对应。
# 之所以不是枚举字符串：半天是"第几个"而不是"是什么"，用 SMALLINT 存更省空间，
# 也能直接参与唯一约束 (vehicle_id, slot_date, half)。
HALVES: Tuple[int, ...] = (0, 1)

HALF_LABEL = {0: "上午", 1: "下午"}

SLOT_STATUSES: Tuple[str, ...] = ("free", "booked", "maintenance")

__all__ = ["HALF_LABEL", "HALVES", "SLOT_STATUSES", "describe_slot", "half_label", "parse_day"]


def half_label(half: int) -> str:
    return HALF_LABEL.get(int(half), f"half={half}")


def parse_day(value: str | date_cls) -> date_cls:
    """接受 'YYYY-MM-DD' 或 date；非法值必须是 400 而不是 500。"""
    if isinstance(value, date_cls):
        return value
    try:
        return date_cls.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def describe_slot(vehicle_id: int, day: date_cls, half: int) -> str:
    """日志/错误信息里统一的人类可读写法：'VEH12 2025-09-01 上午'。"""
    return f"vehicle={vehicle_id} {day} {half_label(half)}"
