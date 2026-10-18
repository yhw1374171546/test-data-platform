"""定时任务接口：`POST /v1/jobs/summary`（技术方案 §三 接口清单）。

它是 worker 的**手动触发版**：线上靠 `scripts/outbox_worker.py` 常驻轮询，
但"跑一次某天的汇总"必须有个 API——演示、补数、排查都要用。

幂等：同 key 同 body 重放返回首次结果；不过这个操作本身也是幂等的
（汇总是重算的，跑 1 次和跑 10 次结果一样），所以幂等键在这里更像"防止重复触发"的护栏。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import require_idempotency_key
from app.api.errors import translate_domain_errors
from app.api.schemas import (ReconJobIn, ReconJobOut, SummaryJobIn,
                             SummaryJobOut)
from app.service.outbox import diff_summary, outbox_stats, process_batch
from app.service.recon import run_recon

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

IdemKey = Annotated[str, Depends(require_idempotency_key)]


@router.post("/summary", response_model=SummaryJobOut,
             summary="处理 outbox 并生成日报汇总（幂等；可只处理某一天）")
def summary_job(payload: SummaryJobIn, idem_key: IdemKey) -> SummaryJobOut:
    biz = str(payload.biz_date) if payload.biz_date else None
    with translate_domain_errors():
        stats = process_batch(limit=payload.limit, biz_date=biz)
        remaining = outbox_stats()
        diff = diff_summary(biz_date=biz)

    return SummaryJobOut(processed=stats["scanned"], done=stats["done"],
                         failed=stats["failed"], dead=stats["dead"],
                         pending_left=remaining["pending"],
                         dead_total=remaining["dead"],
                         diff_count=diff["diff_count"])


@router.post("/recon", response_model=ReconJobOut,
             summary="日终对账：明细 vs 汇总 vs 失败是否提单（幂等；可只对某天/某项目）")
def recon_job(payload: ReconJobIn, idem_key: IdemKey) -> ReconJobOut:
    with translate_domain_errors():
        result = run_recon(biz_date=str(payload.biz_date) if payload.biz_date else None,
                           project_id=payload.project_id)
    return ReconJobOut(**result)
