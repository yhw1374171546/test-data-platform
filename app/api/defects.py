"""问题单接口：提报、状态转移、详情（含审计轨迹）、keyset 列表、状态机自描述。

路由顺序有讲究：`/v1/defects/status-machine` 必须声明在 `/v1/defects/{defect_no}` **之前**，
否则 "status-machine" 会被当成 defect_no 匹配掉——这类 bug 只在联调时才暴露，
所以宁可在这里多写一行注释提醒。
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Response

from app.api.abac import ensure_defect, ensure_project, visible_projects
from app.api.authz import current_user
from app.api.deps import require_idempotency_key
from app.api.errors import AppError, translate_domain_errors
from app.api.schemas import (DefectCreate, DefectCreatedOut, DefectListOut,
                             DefectOut, StatusMachineOut, TransitionIn,
                             TransitionOut)
from app.domain import defect_state
from app.infra import metrics
from app.infra import auth
from app.service.defect import (create_defect, get_defect, list_defects,
                                transition_defect)

router = APIRouter(prefix="/v1/defects", tags=["defect"])

IdemKey = Annotated[str, Depends(require_idempotency_key)]


# ── 只读：状态机自描述（必须在 /{defect_no} 之前）────────────────
@router.get("/status-machine", response_model=StatusMachineOut,
            summary="问题单状态机全貌（转移表 + 8×8 矩阵）")
def status_machine() -> StatusMachineOut:
    """把状态机本身做成接口。

    好处有两个：前端不用硬编码按钮可用性（直接读 allowed_next / matrix）；
    面试演示时打开这个 URL，"非法转移 409"就有了自证的依据。
    """
    return StatusMachineOut(
        statuses=list(defect_state.STATUSES),
        labels=dict(defect_state.STATUS_LABEL),
        settled=sorted(defect_state.SETTLED),
        transitions={src: sorted(dst) for src, dst in defect_state.TRANSITIONS.items()},
        matrix=defect_state.matrix(),
        legal_pairs=[list(p) for p in defect_state.legal_pairs()],
    )


# ── 写：提报 ────────────────────────────────────────────
@router.post("", response_model=DefectCreatedOut, status_code=201,
             summary="人工提报问题单（正常链路由执行失败自动带出）")
def create_defect_api(payload: DefectCreate, response: Response,
                      idem_key: IdemKey,
                      user: Annotated[auth.User, Depends(current_user)]) -> DefectCreatedOut:
    # ABAC：只能在自己参与的项目里提问题单（admin 直通）
    ensure_project(user, payload.project_id, action="提报问题单")
    with translate_domain_errors():
        result = create_defect(idem_key=idem_key,
                               title=payload.title,
                               severity=payload.severity,
                               project_id=payload.project_id,
                               actor=payload.actor,
                               owner_id=payload.owner_id,
                               execution_id=payload.execution_id)

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    response.status_code = result.status_code
    return DefectCreatedOut(defect_no=result.defect_no,
                            defect_id=result.defect_id,
                            status=result.status,
                            code=result.code,
                            replay=result.code == "replayed")


# ── 写：状态转移 ────────────────────────────────────────
@router.post("/{defect_no}/transition", response_model=TransitionOut,
             summary="问题单状态转移（非法 409；重复提交 already=true）")
def transition_api(defect_no: str, payload: TransitionIn,
                   idem_key: IdemKey,
                   user: Annotated[auth.User, Depends(current_user)]) -> TransitionOut:
    # 按单号反查项目再校验：不能信任客户端传的 project_id
    ensure_defect(user, defect_no, action="流转问题单")
    with translate_domain_errors():
        result = transition_defect(idem_key=idem_key,
                                   defect_no=defect_no,
                                   to_status=payload.to_status,
                                   actor=payload.actor,
                                   reason=payload.reason)

    metrics.DEFECT_TRANSITIONS.labels(to_status=result.to_status or "?",
                                      result=result.code).inc()

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    return TransitionOut(defect_no=result.defect_no,
                         from_status=result.from_status,
                         to_status=result.to_status,
                         already=result.already,
                         code=result.code,
                         replay=result.code == "replayed")


# ── 读：列表（keyset 分页）──────────────────────────────
@router.get("", response_model=DefectListOut, summary="问题单列表（keyset 分页）")
def list_defects_api(user: Annotated[auth.User, Depends(current_user)],
                     project_id: Optional[int] = Query(default=None, gt=0),
                     status: Optional[str] = Query(default=None),
                     limit: int = Query(default=20, ge=1, le=100),
                     cursor: Optional[str] = Query(
                         default=None,
                         description="上一页返回的 next_cursor：'ISO时间|id'")) -> DefectListOut:
    scope = visible_projects(user)
    if scope is None:                                  # admin：看全部
        project_ids = {project_id} if project_id is not None else None
    elif project_id is not None:
        ensure_project(user, project_id, action="查看问题单")
        project_ids = {project_id}
    else:
        project_ids = scope                           # 可能为空集合：必须真的过滤成空
    with translate_domain_errors():
        data = list_defects(project_id=project_id, project_ids=project_ids,
                            status=status, limit=limit, cursor=cursor)
    return DefectListOut(**data)


# ── 读：详情 + 审计轨迹 ─────────────────────────────────
@router.get("/{defect_no}", response_model=DefectOut, summary="问题单详情（含审计轨迹）")
def get_defect_api(defect_no: str,
                   user: Annotated[auth.User, Depends(current_user)]) -> DefectOut:
    ensure_defect(user, defect_no, action="查看问题单")
    with translate_domain_errors():
        data = get_defect(defect_no)
    return DefectOut(**data)
