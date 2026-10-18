"""对**运行中的**服务做端到端冒烟：这才是"部署起来能用"的证据。

与 tests/ 的分工：
  · `tests/` 用 TestClient 在进程内跑 —— 验证**代码逻辑**；
  · 本脚本走真实 HTTP（uvicorn + 连接池 + 中间件 + 线程池）—— 验证**部署形态**。
两者的失败含义完全不同：前者失败是逻辑错，后者失败往往是配置/端口/DB 连接的问题。

覆盖的技术点：
  健康检查 → 预约(201) → 同 key 重放(replay) → 换 key 抢同一档期(409)
  → 取消释放 → 释放后可重约 → 问题单提报 → 非法转移(409, 带 allowed)
  → 合法转移(200) → 详情审计轨迹 → 列表 → /metrics 有业务指标

用法：
    python scripts/api_smoke.py --base-url http://127.0.0.1:8000
    python scripts/api_smoke.py            # 默认 http://127.0.0.1:8000

主数据 ID 默认从库里取（取最早的项目/车辆/测试员），所以零配置可跑。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import uuid
from datetime import date, timedelta
from typing import Any, Dict, Optional

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import httpx                                                 # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from app.infra.db import session_scope                       # noqa: E402

GREEN, RED, GRAY, RESET = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


class Checker:
    """把"断言"变成一行行可读输出；最后统一给退出码。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, label: str, condition: bool, extra: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  {GREEN}[OK]{RESET}   {label}" + (f"  {GRAY}{extra}{RESET}" if extra else ""))
        else:
            self.failed += 1
            print(f"  {RED}[FAIL]{RESET} {label}" + (f"  {extra}" if extra else ""))
        return condition

    def section(self, title: str) -> None:
        print(f"\n{GRAY}-- {title}{RESET}")


def resolve_ids(client: httpx.Client, args: argparse.Namespace) -> Dict[str, Any]:
    """不传参时从库里取最早的一条主数据，保证脚本零配置。

    版本要按**项目车型**挑：排队接口会校验 `versions.model = projects.model`，
    拿别的车型的版本会被 409 model_mismatch 拒绝。
    """
    if args.project_id and args.vehicle_id and args.testers_id and args.version_id:
        return {"project_id": args.project_id, "vehicle_id": args.vehicle_id,
                "testers_id": args.testers_id, "version_id": args.version_id}
    with session_scope() as s:
        project = s.execute(text(
            "SELECT id, model FROM projects ORDER BY id LIMIT 1")).one()
        version_id = s.execute(text(
            "SELECT id FROM versions WHERE model = :m ORDER BY id LIMIT 1"),
            {"m": project.model}).scalar_one_or_none()
        if version_id is None:
            raise SystemExit(
                f"{RED}项目 {project.id}（车型 {project.model}）没有可用版本{RESET}\n"
                f"  先跑 .\\dev.cmd init 灌演示数据")
        vehicle = s.execute(text(
            "SELECT id, vin FROM vehicles ORDER BY id LIMIT 1")).one()
        return {
            "project_id": int(project.id),
            "version_id": int(version_id),
            "vehicle_id": int(vehicle.id),
            "vin": vehicle.vin,
            "testers_id": s.execute(text("SELECT id FROM testers ORDER BY id LIMIT 1")).scalar_one(),
        }


def post(client: httpx.Client, path: str, key: str, payload: Any) -> httpx.Response:
    return client.post(path, headers={"Idempotency-Key": key}, json=payload)


def run(args: argparse.Namespace) -> int:
    c = Checker()
    tag = uuid.uuid4().hex[:8]
    # 2100 年往后 100 年内的随机起点：冒烟会往库里留数据，
    # 日期空间太小会让下一次运行撞上自己上次占的档期（真实踩过）。
    offset = int(uuid.uuid4().hex[:8], 16) % 36_000
    day = (date(2100, 1, 1) + timedelta(days=offset)).isoformat()

    print("=" * 74)
    print(f"端到端冒烟 · {args.base_url}")
    print("=" * 74)

    with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
        # ── 运维端点 ──
        c.section("运维端点")
        health = client.get("/healthz")
        c.check("GET /healthz 200", health.status_code == 200, f"{health.text[:60]}")
        ready = client.get("/readyz")
        c.check("GET /readyz 200 且 db=ok",
                ready.status_code == 200 and (ready.json() or {}).get("db") == "ok",
                f"{ready.text[:80]}")

        ids = resolve_ids(client, args)
        print(f"  {GRAY}使用主数据：{ids}{RESET}")

        # ── 预约主链路 ──
        c.section("预约主链路（技术方案 4.1）")
        key1 = f"smoke-{tag}-1"

        # 档期日期必须每次都不同：冒烟会在库里留下占用，
        # 日期撞车就会让"首次预约 201"变成 409（踩过）。
        # 所以取 2100 年往后 100 年范围内的随机日期，并在真撞上时换一天重试。
        created = None
        for step in range(10):
            day = (date(2100, 1, 1) + timedelta(days=offset + step)).isoformat()
            # 只挑预约接口需要的字段：请求模型是 extra="forbid"，
            # 直接把 ids 里多出来的 version_id 铺进去会被 422 拒掉（踩过一次，这正是它的价值）
            body = {"project_id": ids["project_id"], "vehicle_id": ids["vehicle_id"],
                    "testers_id": ids["testers_id"], "slot_date": day, "half": 0}
            created = post(client, "/v1/bookings", key1, body)
            if created.status_code != 409:
                break
        assert created is not None
        c.check("首次预约 201 created", created.status_code == 201,
                f"slot_date={day} {created.text[:100]}")
        booking_no: Optional[str] = (created.json() or {}).get("booking_no")

        replay = post(client, "/v1/bookings", key1, body)
        c.check("同 key 重放：同一个单号 + replay=true",
                replay.status_code == created.status_code
                and (replay.json() or {}).get("booking_no") == booking_no
                and (replay.json() or {}).get("replay") is True,
                replay.text[:120])

        stolen = post(client, "/v1/bookings", f"smoke-{tag}-2", body)
        stolen_body = stolen.json() or {}
        c.check("换 key 抢同一档期 409 slot_not_available",
                stolen.status_code == 409 and stolen_body.get("error") == "slot_not_available",
                stolen.text[:120])
        c.check("错误体带 trace_id", bool(stolen_body.get("trace_id")),
                str(stolen_body.get("trace_id"))[:16])

        # ── 取消与释放 ──
        c.section("取消与档期释放")
        cancel = post(client, f"/v1/bookings/{booking_no}/cancel", f"smoke-{tag}-3", {})
        c.check("取消 200 cancelled",
                cancel.status_code == 200 and (cancel.json() or {}).get("code") == "cancelled",
                cancel.text[:120])

        rebook = post(client, "/v1/bookings", f"smoke-{tag}-4", body)
        c.check("取消后同一档期可复用（部分唯一索引生效）",
                rebook.status_code == 201, rebook.text[:120])

        # 收尾：把重约的那单也取消掉，别在开发库里留下"被占的档期"。
        # 这一条不计入检查项——它是清洁动作，不是被测行为。
        rebook_no = (rebook.json() or {}).get("booking_no")
        if rebook_no:
            post(client, f"/v1/bookings/{rebook_no}/cancel", f"smoke-{tag}-cleanup", {})
            print(f"  {GRAY}[..]   已释放本次占用的档期 {day}{RESET}")

        # ── 问题单状态机 ──
        c.section("问题单状态机（技术方案 4.5）")
        defect = post(client, "/v1/defects", f"smoke-{tag}-5", {
            "title": f"冒烟缺陷 {tag}", "severity": "S2",
            "project_id": ids["project_id"], "actor": "smoke:脚本",
        })
        c.check("提报 201", defect.status_code == 201, defect.text[:120])
        defect_no = (defect.json() or {}).get("defect_no")

        illegal = post(client, f"/v1/defects/{defect_no}/transition", f"smoke-{tag}-6",
                       {"to_status": "closed", "actor": "smoke:脚本"})
        illegal_body = illegal.json() or {}
        c.check("open -> closed 非法转移 409",
                illegal.status_code == 409 and illegal_body.get("error") == "illegal_transition",
                illegal.text[:120])
        c.check("409 的 detail.allowed 给出可去往状态",
                sorted((illegal_body.get("detail") or {}).get("allowed") or [])
                == ["rejected", "triaged"],
                str((illegal_body.get("detail") or {}).get("allowed")))

        moved = post(client, f"/v1/defects/{defect_no}/transition", f"smoke-{tag}-7",
                     {"to_status": "triaged", "actor": "smoke:脚本", "reason": "冒烟"})
        c.check("open -> triaged 200 moved",
                moved.status_code == 200 and (moved.json() or {}).get("code") == "moved",
                moved.text[:120])

        again = post(client, f"/v1/defects/{defect_no}/transition", f"smoke-{tag}-8",
                     {"to_status": "triaged", "actor": "smoke:脚本", "reason": "重复提交"})
        c.check("重复提交同一目标状态：already=true（不重复写审计）",
                again.status_code == 200 and (again.json() or {}).get("already") is True,
                again.text[:120])

        detail = client.get(f"/v1/defects/{defect_no}")
        transitions = (detail.json() or {}).get("transitions") or []
        c.check("详情含审计轨迹：创建 1 条 + 真实转移 1 条",
                detail.status_code == 200 and len(transitions) == 2,
                f"transitions={[(t.get('from_status'), t.get('to_status')) for t in transitions]}")

        listing = client.get("/v1/defects", params={"project_id": ids["project_id"], "limit": 5})
        c.check("列表接口可用（keyset 形态）",
                listing.status_code == 200 and "items" in (listing.json() or {}),
                f"items={len((listing.json() or {}).get('items') or [])}")

        machine = client.get("/v1/defects/status-machine")
        c.check("状态机自描述：15 条合法边",
                machine.status_code == 200 and len((machine.json() or {}).get("legal_pairs") or []) == 15)

        # ── 执行队列（排队 + 抢占领取）──
        c.section("执行队列（技术方案 4.2）")
        pool = client.get("/v1/queue", params={"status": "pending", "limit": 5})
        tasks = (pool.json() or {}).get("items") or []
        c.check("任务池列表 200（只读，不消费演示数据）",
                pool.status_code == 200,
                f"pending={len(tasks)}")

        # 自己排一条任务再领，这样反复跑冒烟也不会把演示任务池耗空
        enqueued = post(client, "/v1/queue", f"smoke-{tag}-task", {
            "project_id": ids["project_id"], "version_id": ids["version_id"],
            "case_no": f"SMOKE-{tag}", "case_title": f"冒烟用例 {tag}",
            "module": "冒烟", "priority": "P2",
        })
        enqueued_body = enqueued.json() or {}
        c.check("排队一条新任务 201（用例不存在则先建）",
                enqueued.status_code == 201 and enqueued_body.get("status") == "pending",
                enqueued.text[:120])

        task_id = enqueued_body.get("task_id")
        task_ver = enqueued_body.get("version", 0)

        claimed = post(client, "/v1/queue/claim", f"smoke-{tag}-claim",
                       {"task_id": task_id, "tester_id": ids["testers_id"]})
        claimed_body = claimed.json() or {}
        c.check("领取 200 claimed（version 只 +1）",
                claimed.status_code == 200 and claimed_body.get("code") == "claimed"
                and claimed_body.get("version") == task_ver + 1,
                f"task={task_id} {claimed.text[:100]}")

        replay_task = post(client, "/v1/queue/claim", f"smoke-{tag}-claim",
                           {"task_id": task_id, "tester_id": ids["testers_id"]})
        c.check("同 key 重放：同版本号 + replay=true",
                replay_task.status_code == 200
                and (replay_task.json() or {}).get("replay") is True
                and (replay_task.json() or {}).get("version") == claimed_body.get("version"),
                replay_task.text[:120])

        released = post(client, f"/v1/queue/{task_id}/release",
                        f"smoke-{tag}-release", {"tester_id": ids["testers_id"]})
        c.check("释放 200 pending（version 再 +1）",
                released.status_code == 200
                and (released.json() or {}).get("status") == "pending"
                and (released.json() or {}).get("version") == task_ver + 2,
                released.text[:120])

        stale = post(client, "/v1/queue/claim", f"smoke-{tag}-stale",
                     {"task_id": task_id, "tester_id": ids["testers_id"],
                      "expect_version": task_ver})
        c.check("旧版本号重领 409 version_conflict（ABA 保护）",
                stale.status_code == 409
                and (stale.json() or {}).get("error") == "version_conflict",
                stale.text[:120])

        fresh = post(client, "/v1/queue/claim", f"smoke-{tag}-fresh",
                     {"task_id": task_id, "tester_id": ids["testers_id"],
                      "expect_version": task_ver + 2})
        c.check("新版本号重领 200 claimed（version 到 v3）",
                fresh.status_code == 200
                and (fresh.json() or {}).get("version") == task_ver + 3,
                fresh.text[:120])

        # ── 提交执行结果（executions + outbox + 缺陷，一个事务）──
        c.section("提交执行结果（技术方案 4.4）")
        submit_key = f"smoke-{tag}-submit"
        submitted = post(client, "/v1/executions", submit_key, {
            "task_id": task_id, "tester_id": ids["testers_id"],
            "result": "fail", "note": "冒烟：左转压线",
            "vehicle_id": ids["vehicle_id"],
            "defect": {"title": f"冒烟缺陷 {tag} · NOA 左转压线", "severity": "S1"},
        })
        submitted_body = submitted.json() or {}
        c.check("提交失败结果 201，带 execution_id / defect_no / biz_date",
                submitted.status_code == 201 and submitted_body.get("code") == "recorded"
                and submitted_body.get("execution_id") and submitted_body.get("defect_no")
                and submitted_body.get("biz_date"),
                submitted.text[:140])

        execution_id = submitted_body.get("execution_id")
        defect_no = submitted_body.get("defect_no")

        replay_submit = post(client, "/v1/executions", submit_key, {
            "task_id": task_id, "tester_id": ids["testers_id"],
            "result": "fail", "note": "冒烟：左转压线",
            "vehicle_id": ids["vehicle_id"],
            "defect": {"title": f"冒烟缺陷 {tag} · NOA 左转压线", "severity": "S1"},
        })
        c.check("同 key 重放：返回同一个 execution_id（不重复落库）",
                replay_submit.status_code == submitted.status_code
                and (replay_submit.json() or {}).get("execution_id") == execution_id
                and (replay_submit.json() or {}).get("replay") is True,
                replay_submit.text[:120])

        linked = client.get(f"/v1/defects/{defect_no}")
        c.check("缺陷能追溯到来源执行记录（execution_id 对得上）",
                linked.status_code == 200
                and (linked.json() or {}).get("execution_id") == execution_id,
                linked.text[:120])

        done_tasks = client.get("/v1/queue", params={"project_id": ids["project_id"],
                                                    "status": "done", "limit": 100})
        c.check("任务已转 done",
                task_id in [t["task_id"] for t in (done_tasks.json() or {}).get("items") or []])

        # ── 日报汇总与 outbox（技术方案 4.4）──
        c.section("日报汇总与 outbox（技术方案 4.4）")
        # 注意用的是**执行的 biz_date**（提交当天），不是上面那个 far-future 的档期日 day。
        # 这里踩过一次：按 day 过滤时一条消息都没查到，断言"差异 0"其实是假通过。
        exec_day = submitted_body.get("biz_date") or day
        job = None
        final: Dict[str, Any] = {}
        for attempt in range(5):
            job = post(client, "/v1/jobs/summary", f"smoke-{tag}-summary-{attempt}",
                       {"biz_date": exec_day, "limit": 500})
            final = job.json() or {}
            if job.status_code != 200 or final.get("pending_left", 0) == 0:
                break
        c.check("POST /v1/jobs/summary 200 且处理掉本轮消息",
                job is not None and job.status_code == 200
                and final.get("done", 0) >= 1,
                f"biz_date={exec_day} done={final.get('done')} "
                f"pending_left={final.get('pending_left')}")
        c.check("本次冒烟这一天的汇总与明细差异 0（最终一致）",
                final.get("diff_count") == 0, f"diff={final.get('diff_count')}")
        c.check("outbox 无死信",
                final.get("dead_total") == 0, f"dead={final.get('dead_total')}")

        admin = client.get("/v1/admin/outbox", params={"limit": 3})
        admin_body = admin.json() or {}
        c.check("GET /v1/admin/outbox 200（能看积压与死信）",
                admin.status_code == 200 and "stats" in admin_body
                and admin_body["stats"].get("summary_rows", 0) >= 1,
                f"stats={ {k: admin_body.get('stats', {}).get(k) for k in ('pending', 'done', 'dead', 'summary_rows')} }")

        # ── 日终对账（技术方案 4.6）──
        c.section("日终对账（技术方案 4.6）")
        recon = post(client, "/v1/jobs/recon", f"smoke-{tag}-recon",
                     {"biz_date": exec_day, "project_id": ids["project_id"]})
        recon_body = recon.json() or {}
        c.check("POST /v1/jobs/recon 200 且真的检查到了数据",
                recon.status_code == 200 and recon_body.get("pairs_checked", 0) >= 1,
                f"pairs_checked={recon_body.get('pairs_checked')} "
                f"（0 说明作用域没选对，'无差异'就是假通过）")
        c.check("本项目当天无差异（失败已提单、汇总对得上）",
                recon_body.get("total") == 0,
                f"total={recon_body.get('total')} by_kind={recon_body.get('by_kind')}")

        dis = client.get("/v1/admin/recon/discrepancies",
                         params={"biz_date": exec_day, "project_id": ids["project_id"]})
        c.check("GET /v1/admin/recon/discrepancies 200（差异单可查）",
                dis.status_code == 200 and "items" in (dis.json() or {}),
                f"items={len((dis.json() or {}).get('items') or [])}")

        # ── 缓存一致性（技术方案 4.3）──
        c.section("缓存一致性（技术方案 4.3）")
        vin = ids["vin"]
        first = client.get(f"/v1/vehicles/{vin}")
        c.check("GET /v1/vehicles/{vin} 200 且带 X-Cache",
                first.status_code == 200
                and first.headers.get("X-Cache") in ("hit", "miss", "bypass"),
                f"X-Cache={first.headers.get('X-Cache')} "
                f"strategy={first.headers.get('X-Cache-Strategy')}")

        second = client.get(f"/v1/vehicles/{vin}")
        c.check("第二次读命中缓存（Redis 可用时必为 hit）",
                second.status_code == 200
                and second.headers.get("X-Cache") == "hit",
                f"X-Cache={second.headers.get('X-Cache')}")

        new_location = f"冒烟场-{tag}"
        patched = client.patch(f"/v1/vehicles/{vin}", json={"location": new_location})
        c.check("PATCH /v1/vehicles/{vin} 200（更新会失效缓存）",
                patched.status_code == 200
                and (patched.json() or {}).get("location") == new_location,
                patched.text[:120])

        after = client.get(f"/v1/vehicles/{vin}")
        c.check("写后读到新值（缓存已失效，X-Cache=miss）",
                after.status_code == 200
                and (after.json() or {}).get("location") == new_location
                and after.headers.get("X-Cache") == "miss",
                f"X-Cache={after.headers.get('X-Cache')}")

        # ── 指标 ──
        c.section("可观测性")
        metrics = client.get("/metrics")
        text_body = metrics.text
        c.check("POST /metrics 200", metrics.status_code == 200)
        c.check("业务指标已打点（booking / defect / claim / execution / outbox / cache）",
                "tdp_booking_attempts_total" in text_body
                and "tdp_defect_transitions_total" in text_body
                and "tdp_task_claim_attempts_total" in text_body
                and "tdp_execution_submits_total" in text_body
                and "tdp_outbox_pending" in text_body
                and "tdp_cache_hits_total" in text_body)

    print("\n" + "=" * 74)
    if c.failed == 0:
        print(f"  {GREEN}结论：全部通过（{c.passed} 项）{RESET}")
        print("=" * 74)
        return 0
    print(f"  {RED}结论：{c.failed} 项失败 / 共 {c.passed + c.failed} 项{RESET}")
    print("=" * 74)
    return 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="对运行中的 test-data-platform 做端到端冒烟")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--project-id", type=int, default=0)
    ap.add_argument("--version-id", type=int, default=0)
    ap.add_argument("--vehicle-id", type=int, default=0)
    ap.add_argument("--testers-id", type=int, default=0)
    args = ap.parse_args(argv)

    try:
        return run(args)
    except httpx.ConnectError:
        print(f"{RED}连不上 {args.base_url}{RESET}\n"
              f"  先起服务：.\\dev.cmd api    （或用 dev.cmd smoke 让它自动起临时服务）",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
