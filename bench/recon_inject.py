"""对账注入实验：**5 种差错全部检出、无误报**（技术方案 §4.6 的实验部分）。

做法：先造一个"完全正确"的项目（每天的明细、汇总、失败提单都对得上），
确认对账**一条都不报**（误报 0）；然后逐天注入不同差错，看是否都能被检出。

注入的 5 种差错：

  ① 漏汇总（missing_summary）：删掉某天的汇总行
  ② 多汇总（extra_summary）：给某天塞一行没有明细的汇总
  ③ 数字改错（summary_mismatch）：把某天的 `passed` 改大
  ④ 失败未提单（fail_without_defect）：提交一条 fail 但不带缺陷
  ⑤ 跨日（同一份数据两边都错）：把某天的执行时间挪到次日
     → 原日 extra_summary（明细没了、汇总还在）、次日 missing_summary（明细来了、汇总没有）

用法：
    python bench/recon_inject.py
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging                                                # noqa: E402

from sqlalchemy import text                                  # noqa: E402

from app.domain.biz_time import TZ                           # noqa: E402
from app.infra.db import session_scope                       # noqa: E402
from app.service.claim import claim_task                     # noqa: E402
from app.service.execution import submit_execution           # noqa: E402
from app.service.outbox import process_batch                 # noqa: E402
from app.service.recon import run_recon                      # noqa: E402

# 本实验就是要制造差异，对账/worker 的 WARNING 是预期噪声，压掉才能看清结果
logging.getLogger("tdp.recon").setLevel(logging.CRITICAL)
logging.getLogger("tdp.outbox").setLevel(logging.CRITICAL)


def seed() -> Dict[str, Any]:
    tag = uuid.uuid4().hex[:8]
    base = date(2024, 1, 1) + timedelta(days=uuid.uuid4().int % 600)
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '对账注入项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-RECON-{tag}"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-BENCH-{tag}"}).scalar_one()
        # 第二个版本专门用来造"有汇总没明细"：它一行执行记录都没有
        vid2 = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-BENCH-{tag}-EMPTY"}).scalar_one()
        tester_id = s.execute(text("""
            INSERT INTO testers (employee_no, name, team)
            VALUES (:e, '对账员', '对账组') RETURNING id
        """), {"e": f"EB-{tag}"}).scalar_one()
    return {"tag": tag, "project_id": int(pid), "version_id": int(vid),
            "version2_id": int(vid2),
            "tester_id": int(tester_id), "base": base}


def make_day(env: Dict[str, Any], day: date, results: List[str],
             defect_for_fail: bool = True) -> List[int]:
    """给某一天造 N 条执行结果（默认 fail 都带缺陷，保证这一天的数据是"干净"的）。

    `case_no` 里带随机后缀：同一天可能被调用多次（先造基线、后补一条"失败未提单"），
    用例号必须唯一，否则会撞 `test_cases.case_no` 的唯一约束。
    """
    execution_ids: List[int] = []
    for i, result in enumerate(results):
        suffix = uuid.uuid4().hex[:6]
        with session_scope() as s:
            cid = s.execute(text("""
                INSERT INTO test_cases (case_no, module, title)
                VALUES (:c, '领航', :t) RETURNING id
            """), {"c": f"TC-{env['tag']}-{day}-{suffix}",
                   "t": f"对账用例 {day} #{i}"}).scalar_one()
            task_id = int(s.execute(text("""
                INSERT INTO execution_queue (project_id, version_id, case_id)
                VALUES (:p, :v, :c) RETURNING id
            """), {"p": env["project_id"], "v": env["version_id"],
                   "c": cid}).scalar_one())

        claim_task(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                   tester_id=env["tester_id"])
        defect = ({"title": f"{day} 失败带单", "severity": "S2"}
                  if result == "fail" and defect_for_fail else None)
        res = submit_execution(idem_key=f"bench-{uuid.uuid4().hex}", task_id=task_id,
                               tester_id=env["tester_id"], result=result,
                               executed_at=_at(day), defect=defect)
        execution_ids.append(int(res.execution_id))

    process_batch(limit=500, biz_date=day.isoformat())        # 生成当天汇总
    return execution_ids


def _at(day: date, hour: int = 10) -> datetime:
    return datetime.combine(day, dtime(hour, 0), tzinfo=TZ)


def recon_pairs(env: Dict[str, Any]) -> Set[Tuple[str, str]]:
    """跑一次全项目对账，返回 {(日, kind)} 集合。"""
    result = run_recon(project_id=env["project_id"])
    return {(item["biz_date"], item["kind"]) for item in result["items"]}


def main() -> int:
    print("=" * 74)
    print("对账注入实验：5 种差错 · 检出率 100% · 误报 0")
    print("=" * 74)

    env = seed()
    base: date = env["base"]
    days = [base + timedelta(days=i) for i in range(7)]      # 1 个干净日 + 6 个注入日
    clean, d1, d2, d3, d4, d5, d6 = days

    failed = 0

    # ── 造基线：6 天数据，全部"正确"（fail 都带缺陷）──
    # d6 故意**不造数据**：跨日注入后它才会"凭空多出明细"，正好制造 missing_summary。
    # 如果 d6 本来就有汇总，那跨日只会让次日变成 summary_mismatch（数字对不上），
    # 两种都合理，但这里想演示的是"少算"那一种。
    make_day(env, clean, ["pass", "pass", "fail"])
    make_day(env, d1, ["pass", "pass"])
    make_day(env, d2, ["pass"])
    make_day(env, d3, ["pass", "fail"])
    make_day(env, d4, ["pass"])
    shift_ids = make_day(env, d5, ["pass"])

    baseline = recon_pairs(env)
    print(f"\n[基线] 6 天数据全部正确 → 差异 {len(baseline)} 条"
          f"（{sorted(baseline)}）")
    ok0 = baseline == set()
    print(f"    {'[OK]' if ok0 else '[FAIL]'} 误报 0：干净数据一条都不报")
    failed += 0 if ok0 else 1
    if not ok0:
        print("        基线就不干净，后面的检出率没有意义，先停下")
        return 1

    # ── 注入 5 种差错 ──
    with session_scope() as s:
        # ① 漏汇总
        s.execute(text("DELETE FROM daily_exec_summary WHERE project_id = :p AND biz_date = :d"),
                  {"p": env["project_id"], "d": d1})
        # ② 多汇总：给"第二个版本"塞一行没有明细的汇总
        #    （同一版本直接插会撞 (日,项目,版本) 唯一约束——那说明数据其实是对的）
        s.execute(text("""
            INSERT INTO daily_exec_summary (biz_date, project_id, version_id,
                                           total, passed, failed, blocked)
            VALUES (:d, :p, :v, 4, 4, 0, 0)
        """), {"d": d2, "p": env["project_id"], "v": env["version2_id"]})
        # ③ 数字改错：把 passed 改大 1
        s.execute(text("""
            UPDATE daily_exec_summary SET total = total + 1, passed = passed + 1
             WHERE project_id = :p AND biz_date = :d
        """), {"p": env["project_id"], "d": d3})
        # ⑤ 跨日：把 d5 的那条执行挪到 d6 凌晨（本地 00:30）
        s.execute(text("""
            UPDATE executions SET executed_at = CAST(:at AS TIMESTAMPTZ) WHERE id = :id
        """), {"at": _at(d6, 0) + timedelta(minutes=30), "id": shift_ids[0]})

    # ④ 失败未提单：d4 提交一条 fail，不带 defect
    make_day(env, d4, ["fail"], defect_for_fail=False)

    found = recon_pairs(env)
    expected: Set[Tuple[str, str]] = {
        (d1.isoformat(), "missing_summary"),
        (d2.isoformat(), "extra_summary"),
        (d3.isoformat(), "summary_mismatch"),
        (d4.isoformat(), "fail_without_defect"),
        (d5.isoformat(), "extra_summary"),        # 明细被挪走，汇总还在
        (d6.isoformat(), "missing_summary"),      # 明细来了，汇总没有
    }

    print("\n[注入] 5 种差错，逐条核对检出情况：")
    labels = [
        ("① 漏汇总", (d1.isoformat(), "missing_summary")),
        ("② 多汇总", (d2.isoformat(), "extra_summary")),
        ("③ 数字改错", (d3.isoformat(), "summary_mismatch")),
        ("④ 失败未提单", (d4.isoformat(), "fail_without_defect")),
        ("⑤ 跨日-原日", (d5.isoformat(), "extra_summary")),
        ("⑤ 跨日-次日", (d6.isoformat(), "missing_summary")),
    ]
    for label, pair in labels:
        hit = pair in found
        print(f"    {'[OK]' if hit else '[FAIL]'} {label:<14} 期望 {pair[1]:<20} "
              f"{'检出' if hit else '未检出'}")
        failed += 0 if hit else 1

    invented = found - expected
    missed = expected - found
    print(f"\n[结论] 检出 {len(expected & found)}/{len(expected)} 条，"
          f"误报 {len(invented)} 条，漏检 {len(missed)} 条")
    ok_all = not invented and not missed
    print(f"    {'[OK]' if ok_all else '[FAIL]'} 检出率 100% 且误报 0")
    failed += 0 if ok_all else 1
    if invented:
        print(f"        误报（这些不该出现）：{sorted(invented)}")

    # ── 差异单长什么样 ──
    result = run_recon(project_id=env["project_id"])
    print(f"\n[差异单] 共 {result['total']} 条（一次对账写入/更新 {result['written']} 行）")
    for item in result["items"]:
        print(f"    {item['biz_date']}  {item['kind']:<20} "
              f"expected={item['expected']} actual={item['actual']}  {item['label']}")

    print("\n" + "=" * 74)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
