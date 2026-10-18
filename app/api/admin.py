"""运维接口：看 outbox 积压、重放死信。

为什么这两个接口要暴露：
`dead > 0` 是**必须被人看到**的状态。只写日志等于没告警——
演示时打开 `/v1/admin/outbox?status=dead` 就能看到"哪几条消息卡住了、卡在什么错上"，
再点一下 replay 就恢复。这比"口头说我们有死信机制"有说服力。
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_idempotency_key
from app.api.schemas import (OutboxListOut, OutboxReplayIn, OutboxReplayOut,
                             ReconListOut)
from app.service.outbox import list_messages, replay_dead
from app.service.recon import list_discrepancies

router = APIRouter(prefix="/v1/admin", tags=["admin"])

IdemKey = Annotated[str, Depends(require_idempotency_key)]


@router.get("/outbox", response_model=OutboxListOut,
            summary="outbox 消息与积压统计（status=pending|done|dead）")
def list_outbox(status: Optional[str] = Query(default=None),
                limit: int = Query(default=20, ge=1, le=200)) -> OutboxListOut:
    data = list_messages(status=status, limit=limit)
    return OutboxListOut(**data)


@router.post("/outbox/replay", response_model=OutboxReplayOut,
             summary="重放死信（attempts 归零，不传 ids 则重放全部）")
def replay_outbox(payload: OutboxReplayIn, idem_key: IdemKey) -> OutboxReplayOut:
    replayed = replay_dead(ids=payload.ids, limit=payload.limit)
    return OutboxReplayOut(replayed=replayed)


@router.get("/recon/discrepancies", response_model=ReconListOut,
            summary="对账差异单（默认只看未解决的）")
def recon_discrepancies(
    biz_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    project_id: Optional[int] = Query(default=None, gt=0),
    resolved: Optional[bool] = Query(default=None, description="true=已解决；不传=全部"),
    limit: int = Query(default=50, ge=1, le=200),
) -> ReconListOut:
    data = list_discrepancies(biz_date=biz_date, project_id=project_id,
                              resolved=resolved, limit=limit)
    return ReconListOut(**data)
