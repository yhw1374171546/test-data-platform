"""请求/响应模型。

两个刻意的选择：

1. **写请求 `extra="forbid"`**：字段名拼错（比如 `half=` 写成 `halve=`）必须立刻 422，
   而不是被静默忽略后产生一个"看起来成功但行为不对"的结果。这类 bug 在联调里最贵。
2. **响应模型和领域模型分开**：service 层返回 dataclass + dict，接入层决定对外字段。
   以后要给前端加字段、或改字段名，不用动业务逻辑。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ────────────────────────── 认证 ──────────────────────────
class TokenRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64, description="账号")
    password: str = Field(min_length=1, max_length=128, description="口令")


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="有效期秒数")
    username: str
    role: str = Field(description="admin | tester | viewer")


# ────────────────────────── 预约 ──────────────────────────
class BookingCreate(StrictModel):
    project_id: int = Field(gt=0, description="项目 ID")
    vehicle_id: int = Field(gt=0, description="测试车辆 ID")
    testers_id: int = Field(gt=0, description="预约人（测试员）ID")
    slot_date: date = Field(description="档期日期（YYYY-MM-DD）")
    half: int = Field(ge=0, le=1, description="半天：0=上午 1=下午")


class BookingOut(BaseModel):
    booking_no: str
    booking_id: Optional[int] = None
    code: str = Field(description="created | cancelled | already | replayed")
    replay: bool = Field(default=False, description="true 表示命中幂等键，未产生新副作用")
    already: bool = Field(default=False, description="true 表示重复提交/已是目标状态")


class SlotOut(BaseModel):
    slot_id: int
    slot_date: str
    half: int
    status: str
    booking_id: Optional[int] = None
    version: int


class SlotsOut(BaseModel):
    vin: str
    model: str
    slot_date: str
    only_free: bool
    slots: List[SlotOut]
    free_count: int


# ────────────────────────── 问题单 ──────────────────────────
class DefectCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    severity: str = Field(pattern=r"^S[1-4]$", description="S1 最高 … S4 最低")
    project_id: int = Field(gt=0)
    owner_id: Optional[int] = Field(default=None, gt=0)
    execution_id: Optional[int] = Field(default=None, gt=0, description="来源执行记录，人工提报可空")
    actor: str = Field(default="system", max_length=64, description="提报人标识，审计里会记下来")


class DefectOut(BaseModel):
    defect_id: int
    defect_no: str
    title: str
    severity: str
    status: str
    project_id: int
    execution_id: Optional[int] = None
    owner_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    allowed_next: List[str] = Field(default_factory=list)
    transitions: List[Dict[str, Any]] = Field(default_factory=list)


class DefectCreatedOut(BaseModel):
    defect_no: str
    defect_id: Optional[int] = None
    status: str = "open"
    code: str
    replay: bool = False


class TransitionIn(StrictModel):
    to_status: str = Field(min_length=1, max_length=32)
    actor: str = Field(default="system", max_length=64)
    reason: str = Field(default="", max_length=500)


class TransitionOut(BaseModel):
    defect_no: str
    from_status: str
    to_status: str
    already: bool = Field(description="true 表示重复提交同一目标状态，未写审计")
    code: str = Field(description="moved | already | replayed")
    replay: bool = False


class DefectListOut(BaseModel):
    items: List[DefectOut]
    next_cursor: Optional[str] = None
    limit: int
    has_more: bool


class StatusMachineOut(BaseModel):
    statuses: List[str]
    labels: Dict[str, str]
    settled: List[str]
    transitions: Dict[str, List[str]]
    matrix: Dict[str, Dict[str, bool]]
    legal_pairs: List[List[str]]


# ────────────────────────── 执行队列 ──────────────────────────
class QueueTaskOut(BaseModel):
    task_id: int
    project_id: int
    version_id: int
    case_id: int
    case_no: str
    case_title: str
    priority: str
    ver: str
    status: str
    claimed_by: Optional[int] = None
    claimed_at: Optional[datetime] = None
    version: int


class QueueListOut(BaseModel):
    items: List[QueueTaskOut]
    next_cursor: Optional[str] = None
    limit: int
    has_more: bool


class ClaimIn(StrictModel):
    task_id: int = Field(gt=0)
    tester_id: int = Field(gt=0, description="领取人（测试员）ID")
    expect_version: Optional[int] = Field(
        default=None, ge=0,
        description="严格乐观锁：带上你看到的 task.version。不传则由服务端读当前版本")


class ClaimOut(BaseModel):
    task_id: Optional[int] = None
    tester_id: Optional[int] = None
    status: str = "claimed"
    version: int = 0
    claimed_at: Optional[datetime] = None
    code: str = Field(description="claimed | replayed")
    replay: bool = Field(default=False, description="true 表示命中幂等键，未重复领取")


class ReleaseIn(StrictModel):
    tester_id: int = Field(gt=0, description="必须与领取人一致，否则 409 not_claim_owner")


class ReleaseOut(BaseModel):
    task_id: Optional[int] = None
    status: str = "pending"
    version: int = 0
    code: str = Field(description="released | replayed")
    replay: bool = False


class TaskCreateIn(StrictModel):
    project_id: int = Field(gt=0)
    version_id: int = Field(gt=0, description="软件版本；必须与项目车型一致")
    case_no: str = Field(min_length=1, max_length=40, description="用例编号，如 TC-NOA-0101")
    case_title: str = Field(min_length=1, max_length=200)
    module: str = Field(default="导入", max_length=32)
    priority: str = Field(default="P2", pattern=r"^P[0-3]$")


class TaskCreatedOut(BaseModel):
    task_id: int
    case_id: int
    status: str = "pending"
    version: int = 0
    code: str = Field(description="enqueued | replayed")
    replay: bool = False


# ────────────────────────── 执行结果 ──────────────────────────
class DefectBriefIn(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    severity: str = Field(pattern=r"^S[1-4]$")
    owner_id: Optional[int] = Field(default=None, gt=0)


class ExecutionIn(StrictModel):
    task_id: int = Field(gt=0)
    tester_id: int = Field(gt=0, description="必须等于任务的 claimed_by")
    result: str = Field(pattern=r"^(pass|fail|blocked|skip)$")
    note: str = Field(default="", max_length=500)
    vehicle_id: Optional[int] = Field(default=None, gt=0, description="实际执行用的车")
    executed_at: Optional[datetime] = Field(
        default=None,
        description="实际执行时间（补录场景必传）。不带时区按 UTC 解释；影响 biz_date 归属")
    defect: Optional[DefectBriefIn] = Field(
        default=None, description="失败时可选带出缺陷；不带则留待日终对账发现")


class ExecutionOut(BaseModel):
    execution_id: int
    task_id: int
    result: str
    biz_date: str = Field(description="归属业务日（按 TDP_TZ 本地自然日，默认 UTC+8）")
    executed_at: datetime
    defect_no: Optional[str] = None
    code: str = Field(description="recorded | replayed")
    replay: bool = False


# ────────────────────────── 定时任务 / outbox ──────────────────────────
class SummaryJobIn(StrictModel):
    biz_date: Optional[date] = Field(default=None, description="只处理这一天的消息；不传则处理全部到期消息")
    limit: int = Field(default=200, ge=1, le=2000, description="本轮最多处理多少条")


class SummaryJobOut(BaseModel):
    processed: int
    done: int
    failed: int
    dead: int
    pending_left: int = Field(description="剩余待处理消息数（>0 说明还有积压）")
    dead_total: int = Field(description="死信总数（>0 必须告警 + 人工重放）")
    diff_count: int = Field(description="汇总与明细的差异条数（0 = 一致）")


class OutboxMessageOut(BaseModel):
    id: int
    topic: str
    status: str
    attempts: int
    next_at: datetime = Field(description="下次可处理时间；插入时等于创建时间，可用来粗看积压时长")
    last_error: str
    payload: Dict[str, Any]


class OutboxListOut(BaseModel):
    items: List[OutboxMessageOut]
    stats: Dict[str, Any]


class OutboxReplayIn(StrictModel):
    ids: Optional[List[int]] = Field(default=None, description="指定消息 id；不传则重放全部 dead")
    limit: int = Field(default=100, ge=1, le=1000)


class OutboxReplayOut(BaseModel):
    replayed: int


# ────────────────────────── 日终对账 ──────────────────────────
class ReconJobIn(StrictModel):
    biz_date: Optional[date] = Field(default=None, description="只对这一天；不传则全量重对")
    project_id: Optional[int] = Field(default=None, gt=0, description="只对某个项目")


class ReconJobOut(BaseModel):
    biz_date: Optional[str] = None
    project_id: Optional[int] = None
    pairs_checked: int = Field(description="真正核对过的 (日, 项目) 组合数")
    pairs_with_diff: int = Field(description="其中发现差异的组合数")
    total: int = Field(description="发现的差异条数")
    by_kind: Dict[str, int] = Field(default_factory=dict)
    written: int = Field(description="写入/更新的差异单行数")
    resolved: int = Field(description="本次自动标记为已解决的差异数")
    items: List[Dict[str, Any]] = Field(default_factory=list)


class DiscrepancyOut(BaseModel):
    id: int
    biz_date: str
    project_id: int
    kind: str
    label: str
    expected: int
    actual: int
    detail: Dict[str, Any]
    resolved: bool
    created_at: datetime


class ReconListOut(BaseModel):
    items: List[DiscrepancyOut]
    limit: int


# ────────────────────────── 主数据详情（带缓存）──────────────────────────
class VehicleOut(BaseModel):
    id: int
    vin: str
    model: str
    plate_no: Optional[str] = None
    status: str
    location: str
    cache: str = Field(description="hit（缓存命中）| miss（回源）| bypass（不缓存/写路径）")


class VehicleUpdateIn(StrictModel):
    """三个字段都可选，但至少要给一个；设的是**绝对值**，所以天然幂等。"""

    status: Optional[str] = Field(
        default=None, pattern="^(available|in_test|maintenance|retired)$",
        description="维护中（maintenance）的车不会被排期")
    location: Optional[str] = Field(default=None, max_length=64)
    plate_no: Optional[str] = Field(default=None, max_length=32)


class TestCaseOut(BaseModel):
    id: int
    case_no: str
    module: str
    title: str
    priority: str
    precondition: str = ""
    steps_summary: str = ""
    cache: str = Field(description="hit | miss | bypass")
