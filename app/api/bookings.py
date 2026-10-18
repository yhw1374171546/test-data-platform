"""预约档期接口：`/v1/bookings`、`/v1/vehicles/{vin}/slots`。

**同步 def 而不是 async def**（本项目所有 endpoint 都这样）：
DB 访问是阻塞的，写在 async def 里会直接卡住事件循环；
写成 def 之后 FastAPI 会把整个函数丢进线程池，1000 个并发请求
表现为"线程池 + 连接池"的排队，而不是事件循环被打死。
这也是 bench/slot_contention.py 能压出数、而线上 P99 不会莫名其妙飙的原因。
"""
from __future__ import annotations

from datetime import date as date_cls
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import require_idempotency_key
from app.api.errors import AppError, translate_domain_errors
from app.api.schemas import BookingCreate, BookingOut, SlotsOut
from app.infra import metrics
from app.service.booking import cancel_booking, create_booking, list_slots

router = APIRouter(prefix="/v1", tags=["booking"])

# 幂等键做成依赖，而不是每个函数里自己读 header：
# 校验逻辑只有一处，新加写接口时不可能"忘了校验"。
IdemKey = Annotated[str, Depends(require_idempotency_key)]


@router.post("/bookings", response_model=BookingOut,
             summary="预约车辆档期（核心并发点）")
def create_booking_api(payload: BookingCreate, response: Response,
                       idem_key: IdemKey) -> BookingOut:
    """成功 201；命中幂等键则重放首次响应（`replay=true`）；档期被占 → 409。"""
    with translate_domain_errors():
        result = create_booking(idem_key=idem_key,
                                project_id=payload.project_id,
                                vehicle_id=payload.vehicle_id,
                                testers_id=payload.testers_id,
                                slot_date=payload.slot_date,
                                half=payload.half)

    metrics.BOOKINGS.labels(result=result.code).inc()

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    response.status_code = result.status_code
    return BookingOut(booking_no=result.booking_no,
                      booking_id=result.booking_id,
                      code=result.code,
                      replay=result.replay,
                      already=bool(result.detail.get("already")))


@router.post("/bookings/{booking_no}/cancel", response_model=BookingOut,
             summary="取消预约（释放档期，可重复调用）")
def cancel_booking_api(booking_no: str, response: Response,
                       idem_key: IdemKey) -> BookingOut:
    with translate_domain_errors():
        result = cancel_booking(idem_key=idem_key, booking_no=booking_no)

    metrics.BOOKINGS.labels(result=result.code).inc()

    if not result.ok:
        raise AppError(result.status_code, result.code, result.detail)

    response.status_code = result.status_code
    return BookingOut(booking_no=result.booking_no,
                      booking_id=result.booking_id,
                      code=result.code,
                      replay=result.replay,
                      already=bool(result.detail.get("already")))


@router.get("/vehicles/{vin}/slots", response_model=SlotsOut,
            summary="查某台车某天的档期（`only_free=true` 只看可约）")
def list_slots_api(vin: str,
                   day: Annotated[date_cls, Query(alias="date",
                                                  description="档期日期 YYYY-MM-DD")],
                   only_free: bool = False) -> SlotsOut:
    with translate_domain_errors():
        data = list_slots(vin=vin, day=day, only_free=only_free)
    return SlotsOut(**data)
