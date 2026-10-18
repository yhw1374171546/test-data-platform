"""日终对账：把三方数据拉出来，用纯规则算出差异，落 `recon_discrepancies`（技术方案 §4.6）。

流程：

    ① 明细侧：executions ⋈ execution_queue，按 (本地日, 项目, 版本) 聚合
    ② 汇总侧：daily_exec_summary
    ③ 质量侧：result='fail' 且**没有任何关联问题单**的执行
    ④ 交给 `domain/recon_rules.reconcile()` 比对（纯函数，可穷举测试）
    ⑤ upsert 差异单；本次没再复现的差异自动置 resolved

两个设计取舍：

**差异单是"活"的，不是"每次追加一条"**
`recon_discrepancies` 上有 `UNIQUE (biz_date, project_id, kind)`，
所以每天每项目每类问题只有一行：重复跑对账只会更新 `expected/actual/detail`，
不会堆出一堆历史噪音（那种表没人看得下去）。
问题修好后再跑一次，`resolved` 自动变 true —— **不需要人工点"已解决"**。

**对账口径必须和汇总口径完全一致**
明细侧用 `biz_date_sql()`（和 worker 写汇总用的是同一个纯函数的 SQL 版），
否则会出现"对账说差 3 条，但两边其实都按自己的口径算对了"这种最恶心的假差异。
"""
from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from app.domain.biz_time import biz_date_sql, biz_day_bounds
from app.domain.recon_rules import (KINDS, KIND_LABEL, Discrepancy,
                                    reconcile)
from app.infra import metrics
from app.infra.db import session_scope

logger = logging.getLogger("tdp.recon")


def run_recon(*, biz_date: Optional[str] = None,
              project_id: Optional[int] = None) -> Dict[str, Any]:
    """跑一次对账。`biz_date`/`project_id` 都可选：不传就是全量重对。"""
    day: Optional[date_cls] = _parse_day(biz_date)

    with session_scope() as s:
        detail = _fetch_detail(s, day, project_id)
        summary = _fetch_summary(s, day, project_id)
        fails = _fetch_fails_without_defect(s, day, project_id)

        found = reconcile(detail=detail, summary=summary, fails=fails)

        # "检查过哪些 (日, 项目)"必须包含**没有差异**的组合：
        # 否则某个问题修好之后，那条旧的差异单永远不会被自动置为 resolved。
        checked = {
            *((row["biz_date"], int(row["project_id"])) for row in detail),
            *((row["biz_date"], int(row["project_id"])) for row in summary),
            *((row["biz_date"], int(row["project_id"])) for row in fails),
        }

        written = _upsert_discrepancies(s, found)
        resolved = _resolve_stale(s, checked, found)

    by_kind: Dict[str, int] = {}
    total = 0
    for discrepancies in found.values():
        for d in discrepancies:
            by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
            total += 1

    for kind in KINDS:
        metrics.RECON_DISCREPANCIES.labels(kind=kind).set(by_kind.get(kind, 0))

    result = {
        "biz_date": day.isoformat() if day else None,
        "project_id": project_id,
        # pairs_checked = 真的核对过多少组合；pairs_with_diff = 其中有多少有问题。
        # 这两个必须分开报：只看前者会以为"检查了 0 个"是正常，只看后者会把"没检查"
        # 误读成"没问题"——冒烟里踩过一次。
        "pairs_checked": len(checked),
        "pairs_with_diff": len(found),
        "total": total,
        "by_kind": by_kind,
        "written": written,
        "resolved": resolved,
        "items": [
            {"biz_date": str(biz), "project_id": pid, **d.as_row(),
             "label": KIND_LABEL.get(d.kind, d.kind)}
            for (biz, pid), discrepancies in sorted(found.items())
            for d in discrepancies
        ],
    }
    if total:
        logger.warning("对账发现 %s 条差异：%s", total, by_kind)
    return result


def list_discrepancies(*, biz_date: Optional[str] = None,
                       project_id: Optional[int] = None,
                       resolved: Optional[bool] = None,
                       limit: int = 50) -> Dict[str, Any]:
    """差异单列表（默认只看未解决的）。"""
    day = _parse_day(biz_date)
    limit = max(1, min(int(limit or 50), 200))
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id, biz_date, project_id, kind, expected, actual,
                   detail, resolved, created_at
              FROM recon_discrepancies
             WHERE (CAST(:biz AS DATE) IS NULL OR biz_date = CAST(:biz AS DATE))
               AND (CAST(:pid AS BIGINT) IS NULL OR project_id = CAST(:pid AS BIGINT))
               AND (CAST(:res AS BOOLEAN) IS NULL OR resolved = CAST(:res AS BOOLEAN))
             ORDER BY biz_date DESC, id DESC
             LIMIT :lim
        """), {"biz": day, "pid": project_id, "res": resolved, "lim": limit}).all()

    return {
        "items": [
            {"id": int(r.id), "biz_date": str(r.biz_date),
             "project_id": int(r.project_id), "kind": r.kind,
             "label": KIND_LABEL.get(r.kind, r.kind),
             "expected": int(r.expected), "actual": int(r.actual),
             "detail": dict(r.detail or {}), "resolved": bool(r.resolved),
             "created_at": r.created_at}
            for r in rows
        ],
        "limit": limit,
    }


# ────────────────────────── 取数 ──────────────────────────
def _fetch_detail(session: Any, day: Optional[date_cls],
                  project_id: Optional[int]) -> List[Dict[str, Any]]:
    where = ["1 = 1"]
    params: Dict[str, Any] = {"pid": project_id}
    if project_id is not None:
        where.append("q.project_id = :pid")
    if day is not None:
        start, end = biz_day_bounds(day)
        where.append("e.executed_at >= :start AND e.executed_at < :end")
        params.update({"start": start, "end": end})

    biz_expr = biz_date_sql("e.executed_at")
    rows = session.execute(text(f"""
        SELECT {biz_expr}                                AS biz_date,
               q.project_id,
               q.version_id,
               count(*)                                  AS total,
               count(*) FILTER (WHERE e.result = 'pass') AS passed,
               count(*) FILTER (WHERE e.result = 'fail') AS failed,
               count(*) FILTER (WHERE e.result = 'blocked') AS blocked
          FROM executions e
          JOIN execution_queue q ON q.id = e.queue_id
         WHERE {' AND '.join(where)}
         GROUP BY 1, 2, 3
    """), params).all()
    return [dict(r._mapping) for r in rows]


def _fetch_summary(session: Any, day: Optional[date_cls],
                   project_id: Optional[int]) -> List[Dict[str, Any]]:
    rows = session.execute(text("""
        SELECT biz_date, project_id, version_id, total, passed, failed, blocked
          FROM daily_exec_summary
         WHERE (CAST(:biz AS DATE) IS NULL OR biz_date = CAST(:biz AS DATE))
           AND (CAST(:pid AS BIGINT) IS NULL OR project_id = CAST(:pid AS BIGINT))
    """), {"biz": day, "pid": project_id}).all()
    return [dict(r._mapping) for r in rows]


def _fetch_fails_without_defect(session: Any, day: Optional[date_cls],
                                project_id: Optional[int]) -> List[Dict[str, Any]]:
    """失败且**两个来源都没有单据**的执行。

    `defects.execution_id` 是"来源"，`defect_links` 是"复现关联"，
    提交结果时两者都会写；用户也可能事后手工把执行挂到别的缺陷上。
    所以这里两边都要查，任一存在就算"已提单"。
    """
    where = ["e.result = 'fail'"]
    params: Dict[str, Any] = {"pid": project_id}
    if project_id is not None:
        where.append("q.project_id = :pid")
    if day is not None:
        start, end = biz_day_bounds(day)
        where.append("e.executed_at >= :start AND e.executed_at < :end")
        params.update({"start": start, "end": end})

    biz_expr = biz_date_sql("e.executed_at")
    rows = session.execute(text(f"""
        SELECT {biz_expr} AS biz_date, q.project_id, e.id AS execution_id
          FROM executions e
          JOIN execution_queue q ON q.id = e.queue_id
         WHERE {' AND '.join(where)}
           AND NOT EXISTS (SELECT 1 FROM defect_links l WHERE l.execution_id = e.id)
           AND NOT EXISTS (SELECT 1 FROM defects d WHERE d.execution_id = e.id)
    """), params).all()
    return [dict(r._mapping) for r in rows]


# ────────────────────────── 落库 ──────────────────────────
def _upsert_discrepancies(session: Any,
                          found: Dict[Tuple[date_cls, int], List[Discrepancy]]) -> int:
    written = 0
    for (biz, pid), discrepancies in found.items():
        for d in discrepancies:
            session.execute(text("""
                INSERT INTO recon_discrepancies
                       (biz_date, project_id, kind, expected, actual, detail)
                VALUES (:biz, :pid, :kind, :expected, :actual, CAST(:detail AS JSONB))
                ON CONFLICT (biz_date, project_id, kind) DO UPDATE
                   SET expected = EXCLUDED.expected,
                       actual   = EXCLUDED.actual,
                       detail   = EXCLUDED.detail,
                       resolved = FALSE          -- 问题又出现了，重新打开
            """), {"biz": biz, "pid": pid, "kind": d.kind,
                   "expected": d.expected, "actual": d.actual,
                   "detail": _json(d.detail)})
            written += 1
    return written


def _resolve_stale(session: Any,
                   checked: set,
                   found: Dict[Tuple[date_cls, int], List[Discrepancy]]) -> int:
    """本次没再复现的差异 → resolved=true。

    只处理"本次实际检查过"的 (日, 项目)：否则跑一天的局部对账会把别的日子的差异也标成已解决。
    `checked` 必须包含**没有差异**的组合——否则问题一修好，旧差异单就永远挂着 unresolved。
    """
    resolved = 0
    for biz, pid in sorted(checked):
        kinds = sorted({d.kind for d in found.get((biz, pid), [])})
        row = session.execute(text("""
            UPDATE recon_discrepancies
               SET resolved = TRUE
             WHERE biz_date = :biz AND project_id = :pid
               AND resolved = FALSE
               AND kind <> ALL(CAST(:kinds AS TEXT[]))
            RETURNING id
        """), {"biz": biz, "pid": pid, "kinds": kinds or [""]}).all()
        resolved += len(row)
    return resolved


# ────────────────────────── 内部 ──────────────────────────
def _parse_day(value: Optional[str]) -> Optional[date_cls]:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def _json(payload: Dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, default=str)
