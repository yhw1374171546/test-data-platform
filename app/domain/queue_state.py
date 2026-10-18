"""执行队列的状态规则：**纯函数**，和 defect_state.py 一样不碰数据库。

队列的主干只有一条：

    pending ──领取──> claimed ──开始跑──> running ──提交结果──> done
       │                 │                    │
       └──取消───────────┴────────────────────┴──> cancelled

和问题单状态机不同，这里**故意不做"显式转移表"**：队列的状态集合小、
分支少，而且真正的并发正确性靠的是 `WHERE status='pending' AND version=?`
那一条 UPDATE，不是靠应用层查表。把"能不能领/能不能放"写成几个谓词函数，
既够用，也避免为了"像状态机"而凭空造出一张 12 条边的表。

两条边界写在这里，省得以后各处判断不一致：
1. **`done` 不能回到 `pending`**：要重跑就新建一条任务（`UNIQUE(project,version,case)`
   本来也只允许一条，重跑意味着新版本）。允许回流会让"执行结果"和"任务"对不上。
2. **`cancelled` 不是删除**：取消只是不再被领，历史审计和 `executions` 还要能查。
"""
from __future__ import annotations

from typing import FrozenSet, Tuple

QUEUE_STATUSES: Tuple[str, ...] = ("pending", "claimed", "running", "done", "cancelled")

STATUS_LABEL = {
    "pending": "待领取",
    "claimed": "已领取",
    "running": "执行中",
    "done": "已完成",
    "cancelled": "已取消",
}

# 只有 pending 能被领取。用 frozenset 而不是字符串比较，调用方传进来脏数据也不会误判成可领。
CLAIMABLE: FrozenSet[str] = frozenset({"pending"})

# "被人占着"的状态：释放只能从这两个走。
OWNED: FrozenSet[str] = frozenset({"claimed", "running"})

# 终态：不可领取、不可释放。week 3 提交执行结果时用它做前置判断。
FINISHED: FrozenSet[str] = frozenset({"done", "cancelled"})

__all__ = ["CLAIMABLE", "FINISHED", "OWNED", "QUEUE_STATUSES", "STATUS_LABEL",
           "UnknownQueueStatus", "can_finish", "can_release", "is_claimable",
           "is_known", "is_owned", "normalize_status"]


class UnknownQueueStatus(Exception):
    """状态名不在枚举里——参数错误（400），不是业务冲突（409）。"""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"未知队列状态：{status!r}；可用值：{', '.join(QUEUE_STATUSES)}")


def normalize_status(status: str) -> str:
    """统一小写去空格；未知则抛 UnknownQueueStatus（查询过滤也要早失败）。"""
    value = str(status or "").strip().lower()
    if value not in QUEUE_STATUSES:
        raise UnknownQueueStatus(str(status))
    return value


def is_known(status: str) -> bool:
    return str(status or "").strip().lower() in QUEUE_STATUSES


def is_claimable(status: str) -> bool:
    return str(status or "").strip().lower() in CLAIMABLE


def is_owned(status: str) -> bool:
    """是否处于"某个测试员持有中"——释放/提交结果的前提。"""
    return str(status or "").strip().lower() in OWNED


def can_release(status: str) -> bool:
    """释放 = 把持有中的任务放回待领取。规则与 is_owned 相同，单独取名是为了可读性。"""
    return is_owned(status)


def can_finish(status: str) -> bool:
    """能不能提交执行结果（claimed 或 running 都行：有人习惯领了就直接跑完）。"""
    return is_owned(status)
