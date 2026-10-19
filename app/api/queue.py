"""执行任务队列接口：排队、任务池列表、抢占领取、释放。

四个接口的分工：

    POST /v1/queue              排队（用例不存在则先建）—— 没有它，任务只能靠种子数据
    GET  /v1/queue              看池子里有哪些任务（前端下拉框要用）
    POST /v1/queue/claim        抢占（核心并发点，幂等必需）
    POST /v1/queue/{id}/release 放回池子（幂等必需）

**没有释放接口，领取的 `version` 就是装饰品**：
只有"领取→释放→再领取"这条路径存在，才能证明乐观锁防的是 ABA，
而不只是"重复点击"（那本来就是幂等键在管的事）。
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Response

from app.api.abac import ensure_project, ensure_task, visible_projects
from app.api.authz import current_user
from app.api.deps import require_idempotency_key
from app.api.errors import AppError, translate_domain_errors
from app.api.schemas import (ClaimIn, ClaimOut, QueueListOut, ReleaseIn,
                             ReleaseOut, TaskCreatedOut, TaskCreateIn)
from app.infra import auth, metrics
from app.service.claim import claim_task, enqueue_task, list_queue, release_task

router = APIRouter(prefix="/v1/queue", tags=["queue"])

IdemKey = Annotated[str, Depends(require_idempotency_key)]


@router.post("", response_model=TaskCreatedOut, status_code=201,
             summary="把一条用例排进任务池（用例不存在则先建）")
def enqueue_api(payload: TaskCreateIn, response: Response,
                idem_key: IdemKey,
                user: Annotated[auth.User, Depends(current_user)]) -> TaskCreatedOut:
    ensure_project(user, payload.project_id, action="排队任务")
    with translate_domain_errors():
        result = enqueue_task(idem_key=idem_key,
                              project_id=payload.project_id,
                              version_id=payload.version_id,
                              case_no=payload.case_no,
                              case_title=payload.case_title,
                              module=payload.module,
                              priority=payload.priority)

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    response.status_code = result.status_code
    return TaskCreatedOut(task_id=int(result.task_id or 0),
                          case_id=int(result.case_id or 0),
                          status=result.status,
                          version=result.version,
                          code=result.code,
                          replay=result.replay)


@router.get("", response_model=QueueListOut, summary="任务池列表（keyset 按 id 翻页）")
def list_queue_api(
    user: Annotated[auth.User, Depends(current_user)],
    project_id: Optional[int] = Query(default=None, gt=0),
    status: Optional[str] = Query(default=None, description="pending|claimed|running|done|cancelled"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None, description="上一页的 next_cursor（任务 id）"),
) -> QueueListOut:
    after_id: Optional[int] = None
    if cursor:
        try:
            after_id = int(str(cursor).strip())
        except ValueError as exc:
            raise AppError(400, "invalid_cursor",
                           {"reason": f"游标应为任务 id：{cursor!r}"}) from exc

    scope = visible_projects(user)
    if scope is None:
        project_ids = {project_id} if project_id is not None else None
    elif project_id is not None:
        ensure_project(user, project_id, action="查看任务池")
        project_ids = {project_id}
    else:
        project_ids = scope

    with translate_domain_errors():
        data = list_queue(project_id=project_id, project_ids=project_ids,
                          status=status, after_id=after_id, limit=limit)
    return QueueListOut(**data)


@router.post("/claim", response_model=ClaimOut,
             summary="领取执行任务（抢占：恰好一人成功，其余 409 already_claimed）")
def claim_api(payload: ClaimIn, idem_key: IdemKey,
              user: Annotated[auth.User, Depends(current_user)]) -> ClaimOut:
    ensure_task(user, payload.task_id, action="领取任务")
    with translate_domain_errors():
        result = claim_task(idem_key=idem_key,
                            task_id=payload.task_id,
                            tester_id=payload.tester_id,
                            expect_version=payload.expect_version)

    metrics.CLAIMS.labels(result=result.code).inc()

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    return ClaimOut(task_id=result.task_id,
                    tester_id=result.tester_id,
                    status=result.status,
                    version=result.version,
                    claimed_at=result.claimed_at,
                    code=result.code,
                    replay=result.replay)


@router.post("/{task_id}/release", response_model=ReleaseOut,
             summary="释放任务回待领取（只有领取人本人可以）")
def release_api(task_id: int, payload: ReleaseIn, idem_key: IdemKey,
                user: Annotated[auth.User, Depends(current_user)]) -> ReleaseOut:
    ensure_task(user, task_id, action="释放任务")
    with translate_domain_errors():
        result = release_task(idem_key=idem_key, task_id=task_id,
                              tester_id=payload.tester_id)

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    return ReleaseOut(task_id=result.task_id,
                      status=result.status,
                      version=result.version,
                      code=result.code,
                      replay=result.replay)
