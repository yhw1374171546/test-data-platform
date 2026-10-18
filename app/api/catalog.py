"""主数据详情接口：车辆 / 用例（技术方案 §4.3）。

两个设计点：

**1. 响应头带 `X-Cache: hit|miss|bypass` 和 `X-Cache-Strategy`**
缓存最容易出的问题是"以为命中了其实没有"（key 拼错、TTL 0、Redis 连不上降级）。
把命中状态直接放进响应头，curl 一下就能确认，不用去翻 Redis。

**2. `PATCH /v1/vehicles/{vin}` 不要求 `Idempotency-Key`**
它设的是**绝对字段值**（status/location/plate_no），同一个 body 调 N 次结果完全一样，
天然幂等；再叠一个幂等键只是多一张表记录，没有价值。
需要幂等键的是"有副作用"的写（创建预约、领取任务、提交结果）——
这个区分写在这里，免得以后有人以为漏了。

顺带补上一个之前只在 DDL 注释里存在的状态：`vehicles.status = maintenance`，
现在终于有入口了（车进厂维修，档期不该再被预约）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Response

from app.api.errors import translate_domain_errors
from app.api.schemas import TestCaseOut, VehicleOut, VehicleUpdateIn
from app.infra import cache
from app.service.catalog import get_case, get_vehicle, update_vehicle

router = APIRouter(prefix="/v1", tags=["catalog"])


def _public(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉 `_cache` 这类内部字段，剩下的才是响应模型字段。"""
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _set_cache_headers(response: Response, data: Dict[str, Any]) -> None:
    response.headers["X-Cache"] = str(data.get("_cache") or "bypass")
    response.headers["X-Cache-Strategy"] = cache.strategy()


@router.get("/vehicles/{vin}", response_model=VehicleOut,
            summary="车辆详情（带缓存；响应头 X-Cache 告诉你是命中还是回源）")
def get_vehicle_api(vin: str, response: Response) -> VehicleOut:
    with translate_domain_errors():
        data = get_vehicle(vin)
    _set_cache_headers(response, data)
    return VehicleOut(**_public(data), cache=str(data.get("_cache") or "bypass"))


@router.patch("/vehicles/{vin}", response_model=VehicleOut,
              summary="更新车辆（会失效缓存；设绝对值所以天然幂等）")
def update_vehicle_api(vin: str, payload: VehicleUpdateIn,
                       response: Response) -> VehicleOut:
    with translate_domain_errors():
        data = update_vehicle(vin=vin, status=payload.status,
                              location=payload.location,
                              plate_no=payload.plate_no)
    # 写路径不经过缓存，但要让调用方知道"缓存刚被失效了"
    _set_cache_headers(response, {"_cache": "bypass"})
    return VehicleOut(**_public(data), cache="bypass")


@router.get("/test-cases/{case_no}", response_model=TestCaseOut,
            summary="用例详情（带缓存；用例标题被导入更新时自动失效）")
def get_case_api(case_no: str, response: Response) -> TestCaseOut:
    with translate_domain_errors():
        data = get_case(case_no)
    _set_cache_headers(response, data)
    return TestCaseOut(**_public(data), cache=str(data.get("_cache") or "bypass"))
