"""日终对账落库测试：四类差异、自动解决、幂等、作用域（技术方案 §4.6）。

对账的核心验收有两条，缺一不可：
    ① **检出率 100%**：注入了差错必须报出来；
    ② **误报 0**：没问题的日期一条都不能报。
第 ② 条更难，也更容易被忽略——一个每天报假差异的对账系统，两周后就没人看了。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from sqlalchemy import text

from app.infra.db import session_scope
from app.service.claim import claim_task
from app.service.execution import submit_execution
from app.service.outbox import process_batch
from app.service.recon import list_discrepancies, run_recon


# ────────────────────────── 小工具 ──────────────────────────
def submit(refs: Dict[str, Any], make_task, idem, result: str, *,
           executed_at=None, defect=None) -> int:
    task_id = make_task()
    claim_task(idem_key=idem(), task_id=task_id, tester_id=refs["tester_id"])
    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result=result,
                           executed_at=executed_at, defect=defect)
    assert res.code == "recorded", res
    return int(res.execution_id)


def build_summary(refs: Dict[str, Any], biz) -> None:
    process_batch(limit=50, biz_date=biz.iso)


def discrepancies_of(refs: Dict[str, Any]) -> List[Any]:
    with session_scope() as s:
        return s.execute(text("""
            SELECT biz_date, kind, expected, actual, detail, resolved
              FROM recon_discrepancies WHERE project_id = :pid ORDER BY kind
        """), {"pid": refs["project_id"]}).all()


def kinds_of(refs: Dict[str, Any], *, resolved: bool = False) -> List[str]:
    return sorted(r.kind for r in discrepancies_of(refs) if bool(r.resolved) == resolved)


# ────────────────────────── 不误报 ──────────────────────────
def test_clean_day_has_no_discrepancy(refs: Dict[str, Any], make_task, idem, biz) -> None:
    for result in ("pass", "fail", "blocked", "skip"):
        defect = {"title": "对账用例缺陷", "severity": "S2"} if result == "fail" else None
        submit(refs, make_task, idem, result, executed_at=biz.at(), defect=defect)
    build_summary(refs, biz)

    result = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert result["total"] == 0, f"干净的一天不该有任何差异，实际：{result['items']}"
    # 两者必须分开看：checked 证明"真的核对了"，with_diff 才是"有几个有问题"
    assert result["pairs_checked"] == 1, "应该核对到 1 个 (日, 项目) 组合"
    assert result["pairs_with_diff"] == 0
    assert discrepancies_of(refs) == []


def test_fail_with_defect_is_not_flagged(refs: Dict[str, Any], make_task,
                                        idem, biz) -> None:
    """失败**提了**单就不算差异——这是 `fail_without_defect` 最容易写成误报的地方。"""
    submit(refs, make_task, idem, "fail", executed_at=biz.at(),
           defect={"title": "已提单的失败", "severity": "S1"})
    build_summary(refs, biz)

    run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert discrepancies_of(refs) == []


# ────────────────────────── 四类差异都要报出来 ──────────────────────────
def test_missing_summary_detected(refs: Dict[str, Any], make_task, idem, biz) -> None:
    for _ in range(2):
        submit(refs, make_task, idem, "pass", executed_at=biz.at())
    build_summary(refs, biz)

    with session_scope() as s:
        s.execute(text("DELETE FROM daily_exec_summary WHERE project_id = :pid"),
                  {"pid": refs["project_id"]})

    result = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert result["by_kind"] == {"missing_summary": 1}
    row = discrepancies_of(refs)[0]
    assert row.kind == "missing_summary"
    assert (int(row.expected), int(row.actual)) == (2, 0), "缺了 2 条明细的汇总"
    assert bool(row.resolved) is False


def test_extra_summary_detected(refs: Dict[str, Any], biz) -> None:
    """有汇总无明细：直接塞一行脏的汇总。"""
    day2 = biz.plus(1)
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO daily_exec_summary
                   (biz_date, project_id, version_id, total, passed, failed, blocked)
            VALUES (:d, :pid, :vid, 5, 5, 0, 0)
        """), {"d": day2.day, "pid": refs["project_id"], "vid": refs["version_id"]})

    result = run_recon(biz_date=day2.iso, project_id=refs["project_id"])
    assert result["by_kind"] == {"extra_summary": 1}
    row = discrepancies_of(refs)[0]
    assert (int(row.expected), int(row.actual)) == (0, 5)


def test_summary_mismatch_detected(refs: Dict[str, Any], make_task, idem, biz) -> None:
    submit(refs, make_task, idem, "pass", executed_at=biz.at())
    submit(refs, make_task, idem, "fail", executed_at=biz.at(),
           defect={"title": "已提单", "severity": "S2"})
    build_summary(refs, biz)

    with session_scope() as s:                       # 数字改错
        s.execute(text("""
            UPDATE daily_exec_summary SET total = total + 1, passed = passed + 1
             WHERE project_id = :pid
        """), {"pid": refs["project_id"]})

    result = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert result["by_kind"] == {"summary_mismatch": 1}
    row = discrepancies_of(refs)[0]
    fields = row.detail["fields"][0]
    assert fields["diff"] == {"total": 1, "passed": 1}, "要指出具体哪几列差了多少"


def test_fail_without_defect_detected(refs: Dict[str, Any], make_task, idem,
                                      biz) -> None:
    execution_id = submit(refs, make_task, idem, "fail", executed_at=biz.at())
    build_summary(refs, biz)

    result = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert result["by_kind"] == {"fail_without_defect": 1}
    row = discrepancies_of(refs)[0]
    assert (int(row.expected), int(row.actual)) == (1, 0)
    assert row.detail["execution_ids"] == [execution_id]


def test_cross_day_shift_is_caught(refs: Dict[str, Any], make_task, idem,
                                   biz) -> None:
    """跨日：把一条执行的执行时间挪到次日。

    这不是"一类新差异"，而是同一份数据在两个业务日都错了：
      原日：明细没了、汇总还在     → extra_summary（多算）
      次日：明细有了、汇总没有     → missing_summary（少算）
    所以对账必须**按天各自核对**，只跑一天只能看到一半。
    """
    execution_id = submit(refs, make_task, idem, "pass", executed_at=biz.at(1))
    build_summary(refs, biz)

    with session_scope() as s:              # 挪到次日 01:00（本地）
        s.execute(text("""
            UPDATE executions
               SET executed_at = executed_at + INTERVAL '23 hours'
             WHERE id = :id
        """), {"id": execution_id})

    result = run_recon(project_id=refs["project_id"])       # 不带日期 → 两天都查
    assert result["by_kind"] == {"extra_summary": 1, "missing_summary": 1}

    one_day = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert one_day["by_kind"] == {"extra_summary": 1}, "只看原日，只能看到一半问题"


# ────────────────────────── 自动解决 + 幂等 + 作用域 ──────────────────────────
def test_discrepancy_resolves_after_fix(refs: Dict[str, Any], make_task, idem,
                                        biz) -> None:
    submit(refs, make_task, idem, "pass", executed_at=biz.at())
    build_summary(refs, biz)
    with session_scope() as s:
        s.execute(text("DELETE FROM daily_exec_summary WHERE project_id = :pid"),
                  {"pid": refs["project_id"]})

    run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert kinds_of(refs) == ["missing_summary"]

    # 修好：把消息重放一次，让 worker 重新算出汇总
    with session_scope() as s:
        s.execute(text("""
            UPDATE outbox SET status = 'pending', attempts = 0, next_at = now()
             WHERE payload ->> 'project_id' = :pid
        """), {"pid": str(refs["project_id"])})
    build_summary(refs, biz)

    second = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    assert second["total"] == 0 and second["resolved"] == 1
    assert kinds_of(refs) == [], "修好后差异应自动置 resolved，不需要人工点"
    assert kinds_of(refs, resolved=True) == ["missing_summary"]


def test_rerun_is_idempotent(refs: Dict[str, Any], make_task, idem, biz) -> None:
    submit(refs, make_task, idem, "fail", executed_at=biz.at())
    build_summary(refs, biz)

    first = run_recon(biz_date=biz.iso, project_id=refs["project_id"])
    second = run_recon(biz_date=biz.iso, project_id=refs["project_id"])

    assert first["total"] == second["total"] == 1
    assert len(discrepancies_of(refs)) == 1, "同 (日,项目,kind) 只能有一行（靠唯一约束）"
    assert second["resolved"] == 0, "问题还在，不能把已打开的差异又标成解决"


def test_scope_does_not_touch_other_projects(refs: Dict[str, Any], make_task,
                                             idem, biz) -> None:
    """跑 A 项目的对账，不能把 B 项目还没修的差异顺手标记成已解决。"""
    submit(refs, make_task, idem, "fail", executed_at=biz.at())
    build_summary(refs, biz)

    other_pid = _create_project()
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO recon_discrepancies (biz_date, project_id, kind, expected, actual)
            VALUES (:d, :pid, 'fail_without_defect', 1, 0)
        """), {"d": biz.day, "pid": other_pid})

    try:
        run_recon(biz_date=biz.iso, project_id=refs["project_id"])
        with session_scope() as s:
            resolved = s.execute(text("""
                SELECT resolved FROM recon_discrepancies WHERE project_id = :pid
            """), {"pid": other_pid}).scalar_one()
        assert bool(resolved) is False, "别的项目不在本次作用域内，不能被改动"
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM recon_discrepancies WHERE project_id = :pid"),
                      {"pid": other_pid})
            s.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": other_pid})


def test_list_discrepancies_filters(refs: Dict[str, Any], make_task, idem,
                                    biz) -> None:
    submit(refs, make_task, idem, "fail", executed_at=biz.at())
    build_summary(refs, biz)
    run_recon(biz_date=biz.iso, project_id=refs["project_id"])

    open_only = list_discrepancies(project_id=refs["project_id"], resolved=False)
    assert len(open_only["items"]) == 1
    assert open_only["items"][0]["label"], "差异要带中文说明，运维才看得懂"
    assert open_only["items"][0]["detail"]["execution_ids"]

    assert list_discrepancies(project_id=refs["project_id"], resolved=True)["items"] == []


def _create_project() -> int:
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        return int(s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '对账隔离项目', 'U8')
            RETURNING id
        """), {"c": f"RECON-{tag}"}).scalar_one())
