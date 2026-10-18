"""提交执行结果接口：`POST /v1/executions`。

接口只有一行调用，但背后是一个四表同事务的写操作（详见 app/service/execution.py）。
这里只负责三件事：入参校验、把业务异常翻译成 HTTP、打点。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import require_idempotency_key
from app.api.errors import AppError, translate_domain_errors
from app.api.schemas import ExecutionIn, ExecutionOut
from app.infra import metrics
from app.service.execution import submit_execution

router = APIRouter(prefix="/v1/executions", tags=["execution"])

IdemKey = Annotated[str, Depends(require_idempotency_key)]


@router.post("", response_model=ExecutionOut, status_code=201,
             summary="提交执行结果（同时写 executions + outbox；失败可带出问题单）")
def submit_execution_api(payload: ExecutionIn, response: Response,
                         idem_key: IdemKey) -> ExecutionOut:
    with translate_domain_errors():
        result = submit_execution(
            idem_key=idem_key,
            task_id=payload.task_id,
            tester_id=payload.tester_id,
            result=payload.result,
            note=payload.note,
            vehicle_id=payload.vehicle_id,
            executed_at=payload.executed_at,
            defect=payload.defect.model_dump() if payload.defect else None,
        )

    metrics.EXECUTIONS.labels(result=payload.result, code=result.code).inc()

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    response.status_code = result.status_code
    return ExecutionOut(execution_id=int(result.execution_id or 0),
                        task_id=int(result.task_id or 0),
                        result=result.result,
                        biz_date=result.biz_date.isoformat() if result.biz_date else "",
                        executed_at=result.executed_at,
                        defect_no=result.defect_no,
                        code=result.code,
                        replay=result.replay)
