"""HTTP 层测试：契约、状态码、错误体形状、幂等头。

重点不是"接口能通"，而是**对外契约**：
· 错误体永远是 `{error, detail, trace_id}`；
· 写接口没带 `Idempotency-Key` → 400，而不是默默执行；
· 同 key 重放 → 同一个响应 + `replay=true`；
· 非法状态转移 → 409，且 `detail.allowed` 告诉调用方下一步能去哪。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.infra.db import session_scope



def _body(resp) -> Any:
    return resp.json()


# ────────────────────────── 运维端点 ──────────────────────────
def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert _body(resp)["status"] == "ok"


def test_readyz_checks_db(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert _body(resp)["db"] == "ok"


def test_metrics_exposes_business_counters(client: TestClient) -> None:
    client.get("/healthz")                       # 先打一次，保证有样本
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "tdp_http_requests_total" in resp.text
    assert "tdp_booking_attempts_total" in resp.text


# ────────────────────────── 预约 ──────────────────────────
def _book(client: TestClient, refs: Dict[str, Any], key: str,
          day: str = "2030-01-01", half: int = 0) -> Any:
    return client.post("/v1/bookings", headers={"Idempotency-Key": key}, json={
        "project_id": refs["project_id"],
        "vehicle_id": refs["vehicle_id"],
        "testers_id": refs["tester_id"],
        "slot_date": day,
        "half": half,
    })


def test_booking_create_and_replay(client: TestClient, refs: Dict[str, Any], idem) -> None:
    key = idem()
    first = _book(client, refs, key)
    assert first.status_code == 201, first.text
    body = _body(first)
    assert body["code"] == "created" and body["replay"] is False
    assert body["booking_no"].startswith("BK")

    second = _book(client, refs, key)
    assert second.status_code == 201
    assert _body(second)["replay"] is True
    assert _body(second)["booking_no"] == body["booking_no"], "重放必须返回同一个单号"


def test_booking_conflict_is_409_slot_not_available(client: TestClient,
                                                   refs: Dict[str, Any], idem) -> None:
    assert _book(client, refs, idem()).status_code == 201

    conflicted = _book(client, refs, idem())        # 换一个幂等键抢同一档期
    assert conflicted.status_code == 409
    body = _body(conflicted)
    assert body["error"] == "slot_not_available"
    assert body["trace_id"], "错误体必须带 trace_id"


def test_missing_idempotency_key_is_400(client: TestClient, refs: Dict[str, Any]) -> None:
    resp = client.post("/v1/bookings", json={
        "project_id": refs["project_id"], "vehicle_id": refs["vehicle_id"],
        "testers_id": refs["tester_id"], "slot_date": "2030-01-02", "half": 0,
    })
    assert resp.status_code == 400
    assert _body(resp)["error"] == "missing_idempotency_key"


def test_validation_error_shape(client: TestClient, refs: Dict[str, Any], idem) -> None:
    resp = client.post("/v1/bookings", headers={"Idempotency-Key": idem()}, json={
        "project_id": refs["project_id"], "vehicle_id": refs["vehicle_id"],
        "testers_id": refs["tester_id"], "slot_date": "2030-01-02", "half": 7,
    })
    assert resp.status_code == 422
    body = _body(resp)
    assert body["error"] == "validation_error"
    assert any(p["field"] == "half" for p in body["detail"])


def test_unknown_vehicle_is_404(client: TestClient, refs: Dict[str, Any], idem) -> None:
    resp = client.post("/v1/bookings", headers={"Idempotency-Key": idem()}, json={
        "project_id": refs["project_id"], "vehicle_id": 999_999_999,
        "testers_id": refs["tester_id"], "slot_date": "2030-01-02", "half": 0,
    })
    assert resp.status_code == 404
    assert _body(resp)["error"] == "vehicle_not_found"


def test_slots_then_cancel_releases_slot(client: TestClient, refs: Dict[str, Any],
                                         idem) -> None:
    day = "2030-02-01"
    created = _book(client, refs, idem(), day=day, half=1)
    assert created.status_code == 201
    booking_no = _body(created)["booking_no"]

    slots = client.get(f"/v1/vehicles/{refs['vin']}/slots", params={"date": day})
    assert slots.status_code == 200
    assert _body(slots)["slots"][0]["status"] == "booked"

    free_only = client.get(f"/v1/vehicles/{refs['vin']}/slots",
                           params={"date": day, "only_free": "true"})
    assert _body(free_only)["slots"] == [], "已被占的档期不该出现在 only_free 里"

    cancel = client.post(f"/v1/bookings/{booking_no}/cancel",
                         headers={"Idempotency-Key": idem()})
    assert cancel.status_code == 200
    assert _body(cancel)["code"] == "cancelled"

    # 取消后同一档期必须能再次预约（部分唯一索引 + 释放档期都生效）
    rebook = _book(client, refs, idem(), day=day, half=1)
    assert rebook.status_code == 201, "取消后档期应可复用"

    # 再取消一次：幂等，不报错
    again = client.post(f"/v1/bookings/{booking_no}/cancel",
                        headers={"Idempotency-Key": idem()})
    assert again.status_code == 200
    assert _body(again)["already"] is True


def test_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": "trace-abc"})
    assert resp.headers["X-Request-ID"] == "trace-abc"


# ────────────────────────── 问题单 ──────────────────────────
def _create_defect(client: TestClient, refs: Dict[str, Any], key: str,
                   title: str = "HMI 提示语错误") -> Any:
    return client.post("/v1/defects", headers={"Idempotency-Key": key}, json={
        "title": title, "severity": "S2", "project_id": refs["project_id"],
        "actor": "qa:api",
    })


def test_defect_lifecycle(client: TestClient, refs: Dict[str, Any], idem) -> None:
    created = _create_defect(client, refs, idem())
    assert created.status_code == 201, created.text
    defect_no = _body(created)["defect_no"]

    illegal = client.post(f"/v1/defects/{defect_no}/transition",
                          headers={"Idempotency-Key": idem()},
                          json={"to_status": "closed", "actor": "qa:api"})
    assert illegal.status_code == 409
    body = _body(illegal)
    assert body["error"] == "illegal_transition"
    assert body["detail"]["from"] == "open"
    assert body["detail"]["allowed"] == ["rejected", "triaged"]

    moved = client.post(f"/v1/defects/{defect_no}/transition",
                        headers={"Idempotency-Key": idem()},
                        json={"to_status": "triaged", "actor": "qa:api", "reason": "分诊"})
    assert moved.status_code == 200
    assert _body(moved)["code"] == "moved"

    detail = client.get(f"/v1/defects/{defect_no}")
    assert detail.status_code == 200
    assert _body(detail)["status"] == "triaged"
    assert len(_body(detail)["transitions"]) == 2, "创建 1 条 + 转移 1 条"


def test_defect_create_is_idempotent(client: TestClient, refs: Dict[str, Any],
                                     idem) -> None:
    key = idem()
    first = _create_defect(client, refs, key)
    second = _create_defect(client, refs, key)
    assert first.status_code == 201 and second.status_code == 201
    assert _body(first)["defect_no"] == _body(second)["defect_no"]
    assert _body(second)["replay"] is True


def test_defect_list_filters_by_project(client: TestClient, refs: Dict[str, Any],
                                       idem) -> None:
    for i in range(3):
        _create_defect(client, refs, idem(), title=f"列表用例 {i}")

    listing = client.get("/v1/defects", params={"project_id": refs["project_id"],
                                               "limit": 2})
    assert listing.status_code == 200
    body = _body(listing)
    assert len(body["items"]) == 2 and body["has_more"] is True
    assert body["next_cursor"]


def test_status_machine_endpoint(client: TestClient) -> None:
    resp = client.get("/v1/defects/status-machine")
    assert resp.status_code == 200
    body = _body(resp)
    assert len(body["statuses"]) == 8
    assert len(body["legal_pairs"]) == 15
    assert body["matrix"]["open"]["triaged"] is True
    assert body["matrix"]["open"]["closed"] is False


def test_unknown_defect_is_404(client: TestClient, idem) -> None:
    resp = client.post("/v1/defects/DF-NOT-EXIST/transition",
                       headers={"Idempotency-Key": idem()},
                       json={"to_status": "triaged"})
    assert resp.status_code == 404
    assert _body(resp)["error"] == "defect_not_found"


def test_unknown_status_is_400_with_known_list(client: TestClient, refs: Dict[str, Any],
                                              idem) -> None:
    """状态名写错是**参数错误(400)**，不是业务冲突(409)：前者改对就行，后者要重读状态。"""
    created = _create_defect(client, refs, idem())
    defect_no = _body(created)["defect_no"]

    resp = client.post(f"/v1/defects/{defect_no}/transition",
                       headers={"Idempotency-Key": idem()},
                       json={"to_status": "done"})
    assert resp.status_code == 400
    body = _body(resp)
    assert body["error"] == "unknown_status"
    assert "triaged" in body["detail"]["known"]


def test_bad_cursor_is_400_not_500(client: TestClient, refs: Dict[str, Any]) -> None:
    resp = client.get("/v1/defects", params={"project_id": refs["project_id"],
                                            "cursor": "garbage"})
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_cursor"


# ────────────────────────── 执行队列 ──────────────────────────
def test_queue_claim_then_list(client: TestClient, refs: Dict[str, Any], make_task,
                               idem) -> None:
    task_id = make_task()

    claimed = client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                          json={"task_id": task_id, "tester_id": refs["tester_id"]})
    assert claimed.status_code == 200, claimed.text
    body = _body(claimed)
    assert body["code"] == "claimed" and body["version"] == 1
    assert body["claimed_at"], "领取时间要回给前端（用于超时释放提示）"

    listing = client.get("/v1/queue", params={"project_id": refs["project_id"],
                                             "status": "claimed"})
    assert listing.status_code == 200
    items = _body(listing)["items"]
    assert [t["task_id"] for t in items] == [task_id]
    assert items[0]["claimed_by"] == refs["tester_id"]


def test_queue_claim_conflict_409(client: TestClient, refs: Dict[str, Any], make_task,
                                  make_testers, idem) -> None:
    task_id = make_task()
    first, second = make_testers(2)

    assert client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                       json={"task_id": task_id, "tester_id": first}).status_code == 200

    conflicted = client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                             json={"task_id": task_id, "tester_id": second})
    assert conflicted.status_code == 409
    body = _body(conflicted)
    assert body["error"] == "already_claimed"
    assert body["detail"]["claimed_by"] == first
    assert body["trace_id"]


def test_queue_release_then_reclaim(client: TestClient, refs: Dict[str, Any], make_task,
                                    idem) -> None:
    task_id = make_task()
    tester = refs["tester_id"]

    client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                json={"task_id": task_id, "tester_id": tester})

    released = client.post(f"/v1/queue/{task_id}/release",
                           headers={"Idempotency-Key": idem()},
                           json={"tester_id": tester})
    assert released.status_code == 200
    assert _body(released)["status"] == "pending"
    assert _body(released)["version"] == 2

    # 拿旧版本号重试 → 409 version_conflict（ABA 保护）
    stale = client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                        json={"task_id": task_id, "tester_id": tester,
                              "expect_version": 0})
    assert stale.status_code == 409
    assert _body(stale)["error"] == "version_conflict"

    fresh = client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                        json={"task_id": task_id, "tester_id": tester,
                              "expect_version": 2})
    assert fresh.status_code == 200 and _body(fresh)["version"] == 3


def test_queue_claim_requires_idempotency_key(client: TestClient, refs: Dict[str, Any],
                                              make_task) -> None:
    task_id = make_task()
    resp = client.post("/v1/queue/claim",
                       json={"task_id": task_id, "tester_id": refs["tester_id"]})
    assert resp.status_code == 400
    assert _body(resp)["error"] == "missing_idempotency_key"


def test_queue_enqueue_http(client: TestClient, refs: Dict[str, Any], idem) -> None:
    payload = {"project_id": refs["project_id"], "version_id": refs["version_id"],
               "case_no": "TC-API-0001", "case_title": "隧道内 NOA 退出",
               "module": "领航", "priority": "P1"}

    created = client.post("/v1/queue", headers={"Idempotency-Key": idem()}, json=payload)
    assert created.status_code == 201, created.text
    task_id = _body(created)["task_id"]
    assert _body(created)["status"] == "pending"

    dup = client.post("/v1/queue", headers={"Idempotency-Key": idem()}, json=payload)
    assert dup.status_code == 409
    assert _body(dup)["error"] == "task_exists"
    assert _body(dup)["detail"]["task_id"] == task_id

    listing = client.get("/v1/queue", params={"project_id": refs["project_id"],
                                             "status": "pending"})
    assert task_id in [t["task_id"] for t in _body(listing)["items"]]


# ────────────────────────── 执行结果 ──────────────────────────
def _enqueue_and_claim(client: TestClient, refs: Dict[str, Any], idem) -> int:
    created = client.post("/v1/queue", headers={"Idempotency-Key": idem()}, json={
        "project_id": refs["project_id"], "version_id": refs["version_id"],
        "case_no": f"TC-API-{idem()[-8:]}", "case_title": "提交结果用例",
    })
    task_id = _body(created)["task_id"]
    claimed = client.post("/v1/queue/claim", headers={"Idempotency-Key": idem()},
                          json={"task_id": task_id, "tester_id": refs["tester_id"]})
    assert claimed.status_code == 200, claimed.text
    return task_id


def test_execution_submit_with_defect(client: TestClient, refs: Dict[str, Any],
                                      idem) -> None:
    task_id = _enqueue_and_claim(client, refs, idem)

    submitted = client.post("/v1/executions", headers={"Idempotency-Key": idem()},
                            json={"task_id": task_id, "tester_id": refs["tester_id"],
                                  "result": "fail", "note": "左转压线",
                                  "vehicle_id": refs["vehicle_id"],
                                  "defect": {"title": "NOA 左转压线", "severity": "S1"}})
    assert submitted.status_code == 201, submitted.text
    body = _body(submitted)
    assert body["code"] == "recorded" and body["result"] == "fail"
    assert body["biz_date"], "提交结果要回 biz_date，方便前端按日归集"
    execution_id, defect_no = body["execution_id"], body["defect_no"]

    detail = client.get(f"/v1/defects/{defect_no}")
    assert detail.status_code == 200
    assert _body(detail)["execution_id"] == execution_id, "缺陷要能追到来源执行记录"

    done = client.get("/v1/queue", params={"project_id": refs["project_id"],
                                          "status": "done"})
    assert task_id in [t["task_id"] for t in _body(done)["items"]], "任务应已转 done"


def test_execution_submit_not_claimed_409(client: TestClient, refs: Dict[str, Any],
                                          make_task, idem) -> None:
    task_id = make_task()                    # 没领取
    resp = client.post("/v1/executions", headers={"Idempotency-Key": idem()},
                       json={"task_id": task_id, "tester_id": refs["tester_id"],
                             "result": "pass"})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "not_claimed"


def test_execution_invalid_result_is_422(client: TestClient, refs: Dict[str, Any],
                                         make_task, idem) -> None:
    task_id = make_task()
    resp = client.post("/v1/executions", headers={"Idempotency-Key": idem()},
                       json={"task_id": task_id, "tester_id": refs["tester_id"],
                             "result": "ok"})
    assert resp.status_code == 422
    assert _body(resp)["error"] == "validation_error"


# ────────────────────────── 汇总任务 / outbox 运维 ──────────────────────────
SUMMARY_DAY = "2024-06-01"


def _submit_on_summary_day(client: TestClient, refs: Dict[str, Any], idem,
                           result: str) -> None:
    task_id = _enqueue_and_claim(client, refs, idem)
    resp = client.post("/v1/executions", headers={"Idempotency-Key": idem()},
                       json={"task_id": task_id, "tester_id": refs["tester_id"],
                             "result": result,
                             "executed_at": f"{SUMMARY_DAY}T10:00:00+08:00"})
    assert resp.status_code == 201, resp.text


def test_summary_job_generates_daily_summary(client: TestClient,
                                             refs: Dict[str, Any], idem) -> None:
    _submit_on_summary_day(client, refs, idem, "pass")
    _submit_on_summary_day(client, refs, idem, "fail")

    job = client.post("/v1/jobs/summary", headers={"Idempotency-Key": idem()},
                      json={"biz_date": SUMMARY_DAY, "limit": 200})
    assert job.status_code == 200, job.text
    body = _body(job)
    assert body["done"] >= 2, "本轮应处理掉本测试造的消息"
    assert body["diff_count"] == 0, "处理完汇总必须和明细一致"

    outbox = client.get("/v1/admin/outbox", params={"status": "done", "limit": 50})
    assert outbox.status_code == 200
    stats = _body(outbox)["stats"]
    assert stats["done"] >= 2 and stats["summary_rows"] >= 1


def test_admin_replay_dead_message(client: TestClient, refs: Dict[str, Any],
                                   idem) -> None:
    """未知 topic 的消息会当场进死信；运维接口能看到并重放它。"""
    with session_scope() as s:
        msg_id = s.execute(text("""
            INSERT INTO outbox (topic, payload) VALUES ('bogus_topic', CAST(:p AS JSONB))
            RETURNING id
        """), {"p": json.dumps({
            "project_id": refs["project_id"], "version_id": refs["version_id"],
            "biz_date": SUMMARY_DAY, "result": "pass",
        })}).scalar_one()

    job = client.post("/v1/jobs/summary", headers={"Idempotency-Key": idem()},
                      json={"biz_date": SUMMARY_DAY})
    assert job.status_code == 200
    assert _body(job)["dead"] >= 1

    dead = client.get("/v1/admin/outbox", params={"status": "dead"})
    assert dead.status_code == 200
    ids = [m["id"] for m in _body(dead)["items"]]
    assert int(msg_id) in ids
    assert _body(dead)["items"][0]["last_error"], "死信必须带错误原因"

    replay = client.post("/v1/admin/outbox/replay", headers={"Idempotency-Key": idem()},
                         json={"ids": [int(msg_id)]})
    assert replay.status_code == 200
    assert _body(replay)["replayed"] == 1

    # 重放后它还是坏消息 → 再跑一次会重新 dead（证明重放不是"假装成功"）
    client.post("/v1/jobs/summary", headers={"Idempotency-Key": idem()},
                json={"biz_date": SUMMARY_DAY})
    dead_again = client.get("/v1/admin/outbox", params={"status": "dead"})
    assert int(msg_id) in [m["id"] for m in _body(dead_again)["items"]]


def test_summary_job_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post("/v1/jobs/summary", json={})
    assert resp.status_code == 400
    assert _body(resp)["error"] == "missing_idempotency_key"


# ────────────────────────── 日终对账 ──────────────────────────
RECON_DAY = "2024-07-01"


def _submit_on(client: TestClient, refs: Dict[str, Any], idem, result: str,
               day: str, *, defect: bool = False) -> None:
    task_id = _enqueue_and_claim(client, refs, idem)
    payload: Dict[str, Any] = {"task_id": task_id, "tester_id": refs["tester_id"],
                               "result": result, "executed_at": f"{day}T10:00:00+08:00"}
    if defect:
        payload["defect"] = {"title": "对账用例缺陷", "severity": "S2"}
    resp = client.post("/v1/executions", headers={"Idempotency-Key": idem()}, json=payload)
    assert resp.status_code == 201, resp.text


def test_recon_detects_fail_without_defect(client: TestClient, refs: Dict[str, Any],
                                          idem) -> None:
    _submit_on(client, refs, idem, "fail", RECON_DAY)          # 失败但没提单
    client.post("/v1/jobs/summary", headers={"Idempotency-Key": idem()},
                json={"biz_date": RECON_DAY, "limit": 200})

    recon = client.post("/v1/jobs/recon", headers={"Idempotency-Key": idem()},
                        json={"biz_date": RECON_DAY, "project_id": refs["project_id"]})
    assert recon.status_code == 200, recon.text
    body = _body(recon)
    assert body["total"] >= 1
    assert body["by_kind"].get("fail_without_defect") == 1
    assert body["items"][0]["detail"]["execution_ids"], "差异单要指出是哪几条执行"

    listing = client.get("/v1/admin/recon/discrepancies",
                         params={"biz_date": RECON_DAY, "project_id": refs["project_id"]})
    assert listing.status_code == 200
    items = _body(listing)["items"]
    assert [i["kind"] for i in items] == ["fail_without_defect"]
    assert items[0]["resolved"] is False and items[0]["label"]


def test_recon_reports_nothing_when_clean(client: TestClient, refs: Dict[str, Any],
                                         idem) -> None:
    """失败提了单、汇总也对得上 → 一条差异都不报（误报 0）。"""
    _submit_on(client, refs, idem, "pass", RECON_DAY)
    _submit_on(client, refs, idem, "fail", RECON_DAY, defect=True)
    client.post("/v1/jobs/summary", headers={"Idempotency-Key": idem()},
                json={"biz_date": RECON_DAY, "limit": 200})

    recon = client.post("/v1/jobs/recon", headers={"Idempotency-Key": idem()},
                        json={"biz_date": RECON_DAY, "project_id": refs["project_id"]})
    assert recon.status_code == 200
    body = _body(recon)
    assert body["total"] == 0, f"干净的一天不该报差异：{body['items']}"
    assert body["by_kind"] == {}


def test_recon_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post("/v1/jobs/recon", json={})
    assert resp.status_code == 400
    assert _body(resp)["error"] == "missing_idempotency_key"


# ────────────────────────── 主数据详情（缓存）──────────────────────────
def test_vehicle_detail_cache_headers(client: TestClient, refs: Dict[str, Any]) -> None:
    first = client.get(f"/v1/vehicles/{refs['vin']}")
    assert first.status_code == 200, first.text
    assert first.headers["X-Cache"] in ("miss", "hit", "bypass")
    assert first.headers["X-Cache-Strategy"]

    second = client.get(f"/v1/vehicles/{refs['vin']}")
    assert second.headers["X-Cache"] == "hit", "第二次必须命中缓存"

    location = f"临港-API-{refs['tag']}"
    patched = client.patch(f"/v1/vehicles/{refs['vin']}", json={"location": location})
    assert patched.status_code == 200, patched.text
    assert _body(patched)["location"] == location

    after = client.get(f"/v1/vehicles/{refs['vin']}")
    assert _body(after)["location"] == location, "写完还读到旧值，说明缓存没失效"
    assert after.headers["X-Cache"] == "miss"


def test_vehicle_patch_validation(client: TestClient, refs: Dict[str, Any]) -> None:
    empty = client.patch(f"/v1/vehicles/{refs['vin']}", json={})
    assert empty.status_code == 400, "一个字段都不给就没意义，应该 400"
    assert _body(empty)["error"] == "invalid_argument"

    bad = client.patch(f"/v1/vehicles/{refs['vin']}", json={"status": "flying"})
    assert bad.status_code == 422

    unknown = client.patch("/v1/vehicles/VIN-NOT-EXIST", json={"location": "x"})
    assert unknown.status_code == 404
    assert _body(unknown)["error"] == "vehicle_not_found"


def test_case_detail_is_cached(client: TestClient, refs: Dict[str, Any],
                               idem) -> None:
    case_no = f"TC-API-CACHE-{refs['tag']}"
    assert client.post("/v1/queue", headers={"Idempotency-Key": idem()}, json={
        "project_id": refs["project_id"], "version_id": refs["version_id"],
        "case_no": case_no, "case_title": "缓存用例",
    }).status_code == 201

    first = client.get(f"/v1/test-cases/{case_no}")
    assert first.status_code == 200 and _body(first)["title"] == "缓存用例"
    assert first.headers["X-Cache"] == "miss"
    assert client.get(f"/v1/test-cases/{case_no}").headers["X-Cache"] == "hit"
