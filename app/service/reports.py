"""字典接口：给前端下拉框用的只读数据。

为什么单开一个 `/v1/meta/options` 而不是让前端串 4 个接口：
一个页面的表单要「项目 / 版本 / 车辆 / 测试员」四个下拉，串 4 次请求既慢又容易半屏空白。
字典类数据变化很慢、量也小，合成一个接口最划算（也是后台管理系统里的常见做法）。

注意：这是**只读**接口，走 viewer 权限；不做分页，因为下拉框本来也不适合放几千条。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.infra.db import session_scope

LIMIT = 200


def _rows(sql: str, **params: Any) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()
    return [dict(r._mapping) for r in rows]


def options() -> Dict[str, Any]:
    return {
        "projects": _rows("""
            SELECT id, code, name, model FROM projects
             ORDER BY id DESC LIMIT :lim
        """, lim=LIMIT),
        "versions": _rows("""
            SELECT id, model, ver FROM versions
             ORDER BY id DESC LIMIT :lim
        """, lim=LIMIT),
        "vehicles": _rows("""
            SELECT id, vin, model, plate_no, location FROM vehicles
             ORDER BY id ASC LIMIT :lim
        """, lim=LIMIT),
        "testers": _rows("""
            SELECT id, employee_no, name FROM testers
             ORDER BY id ASC LIMIT :lim
        """, lim=LIMIT),
    }


def daily_report(biz_date: str, project_id: Optional[int] = None) -> Dict[str, Any]:
    """某一天的日报看板：按 (项目, 版本) 的汇总行 + 合计。可按项目过滤。"""
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT s.biz_date, s.project_id, p.code AS project_code, p.name AS project_name,
                   s.version_id, v.ver AS version_name,
                   s.total, s.passed, s.failed, s.blocked
              FROM daily_exec_summary s
              JOIN projects p ON p.id = s.project_id
              JOIN versions v ON v.id = s.version_id
             WHERE s.biz_date = CAST(:day AS DATE)
               AND (CAST(:pid AS BIGINT) IS NULL OR s.project_id = :pid)
             ORDER BY s.project_id, s.version_id
        """), {"day": biz_date, "pid": project_id}).all()
    items = [dict(r._mapping) for r in rows]
    totals = {k: sum(int(i[k] or 0) for i in items) for k in ("total", "passed", "failed", "blocked")}
    # skip 不单独存列，用 total - 其余三项算出来（这是汇总表的既定口径）
    totals["skipped"] = totals["total"] - totals["passed"] - totals["failed"] - totals["blocked"]
    return {"biz_date": biz_date, "items": items, "totals": totals}
