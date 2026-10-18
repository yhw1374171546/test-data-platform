"""问题单状态机：**纯函数，不碰数据库，也不碰 HTTP**（技术方案 §4.5）。

为什么非要抽成纯函数：
状态机是整个项目里最容易写错、也最值得穷举验证的一段逻辑。
一旦它和 SQL、事务混在一个函数里，"哪些转移合法"就只能靠集成测试碰运气；
抽出来之后，可以直接把 8×8 = 64 个 (from, to) 组合全部跑一遍钉死（见 tests/test_defect_state.py）。

主流程（方案原文）：

    open → triaged → assigned → fixing → verifying → closed
      ↓        ↓         ↓         ↓         ↓
    rejected  rejected  rejected  reopened  reopened

图里没画出来的三条支线是按业务常识补的，理由写在这（而不是只写在文档里）：

    reopened → assigned   回归不通过，要重新指派给开发，回到主流程而不是另起一条链
    rejected → closed     误报/不修，确认后关单
    rejected → open       驳回后提报人补充信息重开（"驳回"不等于"销毁"）
    closed   → reopened   线上/后续版本复现，允许再打开
                          —— "终态能不能被推翻"是业务问题，这里选择允许，
                             因为封版后又复现的情况在整车测试里非常常见

两个刻意不做的设计：
1. **不做 `status → status` 的自环**。重复提交同一个 to_status 属于"幂等重放"，
   由 service 层识别成 `already=true`，不写审计——见 app/service/defect.py。
   如果这里允许自环，审计表就会被重放请求灌满，反而看不出真实流转。
2. **不在这里校验角色权限**（谁能 close）。角色是接入层的事，状态机只回答
   "这个状态能不能走到那个状态"。两者混在一起会让矩阵测试变成权限测试。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Mapping, Tuple

# 顺序即业务流程顺序，前端/文档直接复用来渲染进度条
STATUSES: Tuple[str, ...] = (
    "open", "triaged", "assigned", "fixing",
    "verifying", "closed", "rejected", "reopened",
)

STATUS_LABEL: Mapping[str, str] = {
    "open": "待分诊",
    "triaged": "已分诊",
    "assigned": "已指派",
    "fixing": "修复中",
    "verifying": "待验证",
    "closed": "已关闭",
    "rejected": "已驳回",
    "reopened": "重新打开",
}

# 显式转移表：这是本模块唯一的"真相"，没有 if-else 拼出来的隐式规则。
# frozenset 而不是 list：判重是 O(1)，且天然不可变，防止被调用方就地改坏。
TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    "open":      frozenset({"triaged", "rejected"}),
    "triaged":   frozenset({"assigned", "rejected"}),
    "assigned":  frozenset({"fixing", "rejected"}),
    "fixing":    frozenset({"verifying", "reopened"}),
    "verifying": frozenset({"closed", "reopened"}),
    "reopened":  frozenset({"assigned", "rejected"}),
    "rejected":  frozenset({"closed", "open"}),
    "closed":    frozenset({"reopened"}),
}

# "已结单"集合：给日报/看板用（不是终态，closed/rejected 仍可再打开）
SETTLED: FrozenSet[str] = frozenset({"closed", "rejected"})


class UnknownStatus(Exception):
    """状态名不在枚举里——是调用方参数错误，不是业务冲突。"""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"未知问题单状态：{status!r}；可用值：{', '.join(STATUSES)}")


class IllegalTransition(Exception):
    """业务上不允许的转移（如 open → closed 跳步）。调用方应得到 409 而不是 500。"""

    def __init__(self, from_status: str, to_status: str):
        self.from_status = from_status
        self.to_status = to_status
        self.allowed = sorted(TRANSITIONS.get(from_status, frozenset()))
        super().__init__(
            f"不允许从 {from_status} 转移到 {to_status}；"
            f"{from_status} 可去往：{', '.join(self.allowed) or '（无）'}"
        )


def is_known(status: str) -> bool:
    return status in TRANSITIONS


def normalize(status: str) -> str:
    """统一小写去空格；未知则抛 UnknownStatus（早失败，别带着脏状态往下走）。"""
    value = str(status or "").strip().lower()
    if not is_known(value):
        raise UnknownStatus(str(status))
    return value


def allowed_next(from_status: str) -> FrozenSet[str]:
    """当前状态可去往的集合；未知状态返回空集而不是抛异常（便于做"推荐动作"接口）。"""
    return TRANSITIONS.get(str(from_status or "").strip().lower(), frozenset())


def can_transition(from_status: str, to_status: str) -> bool:
    """唯一判定入口。注意自环返回 False——重复提交由 service 层识别为 already。"""
    src = str(from_status or "").strip().lower()
    dst = str(to_status or "").strip().lower()
    if src == dst:
        return False
    return dst in TRANSITIONS.get(src, frozenset())


def check_transition(from_status: str, to_status: str) -> None:
    """校验并抛异常：服务层用它把"非法转移"变成一次明确失败（事务回滚）。"""
    src = normalize(from_status)
    dst = normalize(to_status)
    if src == dst:
        raise IllegalTransition(src, dst)          # 自环交给 service 判 already，走到这里就是误用
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def legal_pairs() -> Tuple[Tuple[str, str], ...]:
    """所有合法 (from, to)，按 STATUSES 顺序展开——测试与文档都用它。"""
    return tuple(
        (src, dst)
        for src in STATUSES
        for dst in STATUSES
        if can_transition(src, dst)
    )


def matrix() -> Dict[str, Dict[str, bool]]:
    """完整 8×8 布尔矩阵，给 `GET /v1/defects/status-machine` 直接当接口返回。"""
    return {
        src: {dst: can_transition(src, dst) for dst in STATUSES}
        for src in STATUSES
    }
