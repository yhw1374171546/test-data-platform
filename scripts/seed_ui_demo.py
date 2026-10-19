"""造一套「给人看」的演示数据（前端截图 / 面试演示用）。

为什么单独写一个脚本，而不是复用 init_db 的随机数据：
init_db 那套数据量很大（十几万条队列），页面打开后满屏是不认识的编号；
演示要的是**一眼能看懂的一屏**：一个项目、一个版本、5 条用例、几条执行结果。

做的事：
  1. 建一个演示项目 + 一个版本（已存在就复用）；
  2. 5 条用例各排一个任务，其中 3 条被领取并提交结果（2 通过 1 失败）；
  3. 失败的那条挂一张问题单；
  4. 写一行日报汇总（真实链路是 worker 重算，这里直接落库，只为了页面有数）；
  5. 给演示车辆铺明后两天的档期，并占掉其中一个。

幂等：重复执行只会跳过已存在的数据（用 code / 唯一键判重）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import text  # noqa: E402

from app.domain.biz_time import TZ  # noqa: E402
from app.infra.db import session_scope  # noqa: E402

PROJECT_CODE = "U8-CITY-NOA-2025Q4"
PROJECT_NAME = "城市 NOA 四季度回归"
MODEL = "U8"
VERSION = "V2.0.0"
CASE_NOS = ["TC-NOA-0101", "TC-NOA-0102", "TC-NOA-0103", "TC-PARK-0201", "TC-HMI-0301"]
PASSED = ["TC-NOA-0101", "TC-NOA-0102"]
FAILED = ["TC-NOA-0103"]


def main() -> int:
    today = datetime.now(TZ).date()
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:code, :name, :model)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """), {"code": PROJECT_CODE, "name": PROJECT_NAME, "model": MODEL}).scalar_one()

        vid = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES (:model, :ver)
            ON CONFLICT (model, ver) DO NOTHING RETURNING id
        """), {"model": MODEL, "ver": VERSION}).scalar()
        if vid is None:
            vid = s.execute(text("SELECT id FROM versions WHERE model=:m AND ver=:v"),
                            {"m": MODEL, "v": VERSION}).scalar_one()

        vehicle_id = s.execute(text("SELECT id FROM vehicles ORDER BY id LIMIT 1")).scalar_one()
        tester_id = s.execute(text("SELECT id FROM testers ORDER BY id LIMIT 1")).scalar_one()

        # 用例是全局的：按编号取 id
        cases = {r.case_no: r.id for r in s.execute(
            text("SELECT id, case_no FROM test_cases WHERE case_no = ANY(:no)"),
            {"no": CASE_NOS}).all()}

        task_ids = {}
        for no in CASE_NOS:
            cid = cases.get(no)
            if not cid:
                continue
            row = s.execute(text("""
                INSERT INTO execution_queue (project_id, version_id, case_id, status, claimed_by, claimed_at)
                VALUES (:p, :v, :c, 'pending', NULL, NULL)
                ON CONFLICT (project_id, version_id, case_id) DO NOTHING
                RETURNING id
            """), {"p": pid, "v": vid, "c": cid}).scalar()
            if row is None:
                row = s.execute(text("""
                    SELECT id FROM execution_queue
                     WHERE project_id=:p AND version_id=:v AND case_id=:c
                """), {"p": pid, "v": vid, "c": cid}).scalar_one()
            task_ids[no] = row

        # 说明：这里**只排任务、不写执行结果**。
        # 执行结果必须走 submit_execution（execution + outbox 同一事务），否则
        # 日报汇总不会更新、日终对账会立刻报差异 —— 演示数据也要遵守这个约定。
        # 想在页面上看到"已执行"的数据：dev.cmd worker 跑一轮即可。

    print(f"[demo] 项目 {PROJECT_CODE} id={pid} 版本 {VERSION}："
          f"用例 {len(cases)} 个，待领取任务 {len(task_ids)} 条")

    # 档期：用**真实的下单服务**占掉明天下午（档期行由服务在事务里物化，
    # 不要手写 vehicle_slots —— 它没有 project_id 列，关系和 booking 在一起）
    from app.service.booking import create_booking

    slot_day = today + timedelta(days=1)
    try:
        result = create_booking(idem_key=f"demo-seed-{slot_day}-1", project_id=pid,
                                vehicle_id=vehicle_id, testers_id=tester_id,
                                slot_date=slot_day, half=1)
        print(f"[demo] 已占用档期 {slot_day} 下午（booking={result.booking_no}）")
    except Exception as exc:                       # noqa: BLE001 - 重复执行时档期已被占，属正常
        print(f"[demo] 档期跳过：{exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
