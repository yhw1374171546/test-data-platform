"""前端配套的只读接口：下拉字典 + 日报看板。

这两个接口是纯读的，权限放到 viewer；真正的写操作仍然走原来的业务接口。
"""
from __future__ import annotations

from datetime import date as date_cls
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.api.authz import current_user
from app.api.errors import AppError
from app.infra import auth
from app.service import reports

router = APIRouter(prefix="/v1", tags=["meta"])


@router.get("/meta/options", summary="下拉字典（项目 / 版本 / 车辆 / 测试员）")
def options(user: Annotated[auth.User, Depends(current_user)]) -> Dict[str, Any]:
    return reports.options()


@router.get("/reports/daily", summary="日报看板（按日期取汇总）")
def daily_report(
    user: Annotated[auth.User, Depends(current_user)],
    biz_date: str = Query(default="", description="业务日期 YYYY-MM-DD，默认今天"),
    project_id: Optional[int] = Query(default=None, gt=0, description="只看某个项目"),
) -> Dict[str, Any]:
    day = (biz_date or "").strip() or date_cls.today().isoformat()
    try:
        date_cls.fromisoformat(day)
    except ValueError as exc:
        raise AppError(400, "invalid_argument",
                       {"reason": f"biz_date 应为 YYYY-MM-DD：{day!r}"}) from exc
    return reports.daily_report(day, project_id)
