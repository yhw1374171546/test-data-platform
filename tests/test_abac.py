"""ABAC：行级权限（按项目隔离）。

RBAC 只回答"这个人能不能写"，ABAC 还要回答"这条数据是不是他的项目"。
很多项目的漏洞就在这一层：一个 tester 能改别人项目的档期，只因为"他是 tester"。

这里覆盖三类攻击面：
  1. 显式传别人的 project_id（提报、排队、预约）
  2. 只给资源 ID（task_id / defect_no / booking_no），不给 project_id —— 必须自己反查
  3. 列表接口不传 project_id —— 必须按成员关系过滤，**空成员关系要真的返回空**
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.infra.auth import issue_token
from app.infra.db import session_scope
from app.main import create_app
from app.service import members


def _client(username: str, role: str) -> TestClient:
    token, _ = issue_token(username, role, ttl_seconds=600)
    return TestClient(create_app(), raise_server_exceptions=False,
                      headers={"Authorization": f"Bearer {token}"})


@pytest.fixture()
def other_project(refs: Dict[str, Any]) -> Iterator[int]:
    """一个"别人的项目"：同样有车辆/版本/用例，但 alice 不是成员。"""
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = int(s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '别的项目', 'U8')
            RETURNING id
        """), {"c": f"ABAC-OTHER-{tag}"}).scalar_one())
        vid = int(s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-ABAC-{tag}"}).scalar_one())
        cid = int(s.execute(text("""
            INSERT INTO test_cases (case_no, module, title)
            VALUES (:c, '领航', '别人的用例') RETURNING id
        """), {"c": f"TC-ABAC-{tag}"}).scalar_one())
        s.execute(text("""
            INSERT INTO execution_queue (project_id, version_id, case_id)
            VALUES (:p, :v, :c)
        """), {"p": pid, "v": vid, "c": cid})
        defect_no = f"DF-ABAC-{tag}"
        s.execute(text("""
            INSERT INTO defects (defect_no, title, severity, status, project_id)
            VALUES (:no, '别人的问题单', 'S2', 'open', :pid)
        """), {"no": defect_no, "pid": pid})
    yield pid
    with session_scope() as s:
        # 删除顺序要管：defect_transitions / defect_links 都引用 defects（提报时会写一条初始审计）；
        # 版本也要按 id 精确删（按 LIKE 删会误伤别的用例还在引用的行）
        s.execute(text("""
            DELETE FROM defect_transitions
             WHERE defect_id IN (SELECT id FROM defects WHERE project_id = :p)
        """), {"p": pid})
        s.execute(text("""
            DELETE FROM defect_links
             WHERE defect_id IN (SELECT id FROM defects WHERE project_id = :p)
        """), {"p": pid})
        s.execute(text("DELETE FROM defects WHERE project_id = :p"), {"p": pid})
        s.execute(text("DELETE FROM execution_queue WHERE project_id = :p OR version_id = :v"),
                  {"p": pid, "v": vid})
        s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        s.execute(text("DELETE FROM versions WHERE id = :v"), {"v": vid})


@pytest.fixture()
def alice(refs: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """一个 tester，只属于 refs 那个项目。"""
    username = f"alice-{refs['tag']}"
    members.add_member(refs["project_id"], username)
    client = _client(username, "tester")
    yield {"username": username, "client": client, "project_id": refs["project_id"]}
    with session_scope() as s:
        s.execute(text("DELETE FROM project_members WHERE username = :u"), {"u": username})


@pytest.fixture()
def stranger(refs: Dict[str, Any]) -> Iterator[TestClient]:
    """一个不属于任何项目的 tester（新入职还没加项目）。"""
    username = f"nobody-{refs['tag']}"
    client = _client(username, "tester")
    yield client
    with session_scope() as s:
        s.execute(text("DELETE FROM project_members WHERE username = :u"), {"u": username})


# ────────────────────────── 成员关系本身 ──────────────────────────
def test_membership_crud(refs: Dict[str, Any]) -> None:
    name = f"m-{refs['tag']}"
    assert members.is_member(name, refs["project_id"]) is False
    members.add_member(refs["project_id"], name)
    members.add_member(refs["project_id"], name)                 # 幂等
    assert members.is_member(name, refs["project_id"]) is True
    assert refs["project_id"] in members.projects_of(name)
    assert [m["username"] for m in members.list_members(refs["project_id"])].count(name) == 1
    assert members.remove_member(refs["project_id"], name) is True
    assert members.is_member(name, refs["project_id"]) is False


def test_project_lookup_by_resource(refs: Dict[str, Any], other_project: int) -> None:
    with session_scope() as s:
        task_id = int(s.execute(text(
            "SELECT id FROM execution_queue WHERE project_id = :p LIMIT 1"),
            {"p": other_project}).scalar_one())
        defect_no = str(s.execute(text(
            "SELECT defect_no FROM defects WHERE project_id = :p LIMIT 1"),
            {"p": other_project}).scalar_one())
    assert members.project_of_task(task_id) == other_project
    assert members.project_of_defect(defect_no) == other_project
    assert members.project_of_task(10 ** 12) is None              # 不存在 → None，交给 404


# ────────────────────────── 1) 显式传别人的 project_id ──────────────────────────
def test_stranger_cannot_book_other_project(alice, refs, other_project) -> None:
    resp = alice["client"].post(
        "/v1/bookings",
        headers={"Idempotency-Key": f"abac-book-{refs['tag']}"},
        json={"project_id": other_project, "vehicle_id": refs["vehicle_id"],
              "testers_id": refs["tester_id"], "slot_date": "2036-05-01", "half": 0})
    assert resp.status_code == 403
    assert resp.json()["error"] == "project_forbidden"
    assert resp.json()["detail"]["action"] == "预约档期"


def test_member_can_book_own_project(alice, refs) -> None:
    resp = alice["client"].post(
        "/v1/bookings",
        headers={"Idempotency-Key": f"abac-book-own-{refs['tag']}"},
        json={"project_id": refs["project_id"], "vehicle_id": refs["vehicle_id"],
              "testers_id": refs["tester_id"], "slot_date": "2036-05-02", "half": 0})
    assert resp.status_code == 201, resp.text


def test_stranger_cannot_enqueue_in_other_project(stranger, refs, other_project) -> None:
    resp = stranger.post(
        "/v1/queue", headers={"Idempotency-Key": f"abac-q-{refs['tag']}"},
        json={"project_id": other_project, "version_id": refs["version_id"],
              "case_no": f"TC-X-{refs['tag']}", "case_title": "越权排队"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "project_forbidden"


def test_stranger_cannot_report_defect_in_other_project(stranger, other_project) -> None:
    resp = stranger.post(
        "/v1/defects", headers={"Idempotency-Key": f"abac-d-{uuid.uuid4().hex}"},
        json={"title": "越权提单", "severity": "S2", "project_id": other_project})
    assert resp.status_code == 403


# ────────────────────────── 2) 只给资源 ID（必须反查）──────────────────────────
def test_stranger_cannot_claim_other_task(stranger, other_project) -> None:
    with session_scope() as s:
        task_id = int(s.execute(text(
            "SELECT id FROM execution_queue WHERE project_id = :p LIMIT 1"),
            {"p": other_project}).scalar_one())
    resp = stranger.post("/v1/queue/claim",
                         headers={"Idempotency-Key": f"abac-claim-{uuid.uuid4().hex}"},
                         json={"task_id": task_id, "tester_id": 1})
    assert resp.status_code == 403
    assert resp.json()["detail"]["action"] == "领取任务"


def test_stranger_cannot_submit_other_task(stranger, other_project) -> None:
    with session_scope() as s:
        task_id = int(s.execute(text(
            "SELECT id FROM execution_queue WHERE project_id = :p LIMIT 1"),
            {"p": other_project}).scalar_one())
    resp = stranger.post("/v1/executions",
                         headers={"Idempotency-Key": f"abac-exec-{uuid.uuid4().hex}"},
                         json={"task_id": task_id, "tester_id": 1, "result": "pass"})
    assert resp.status_code == 403


def test_stranger_cannot_read_or_transition_other_defect(stranger, other_project) -> None:
    with session_scope() as s:
        defect_no = str(s.execute(text(
            "SELECT defect_no FROM defects WHERE project_id = :p LIMIT 1"),
            {"p": other_project}).scalar_one())
    detail = stranger.get(f"/v1/defects/{defect_no}")
    assert detail.status_code == 403
    moved = stranger.post(f"/v1/defects/{defect_no}/transition",
                          headers={"Idempotency-Key": f"abac-tr-{uuid.uuid4().hex}"},
                          json={"to_status": "triaged", "actor": "stranger"})
    assert moved.status_code == 403


# ────────────────────────── 3) 列表接口的可见范围 ──────────────────────────
def test_list_is_filtered_by_membership(alice, refs, other_project) -> None:
    """同名问题单在两个项目里各一条：alice 只应该看到自己项目的。"""
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO defects (defect_no, title, severity, status, project_id)
            VALUES (:no, '自己的问题单', 'S2', 'open', :p)
        """), {"no": f"DF-OWN-{refs['tag']}", "p": refs["project_id"]})

    resp = alice["client"].get("/v1/defects?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "自己的项目里应该能看到问题单"
    assert {i["project_id"] for i in items} == {refs["project_id"]}


def test_list_with_other_project_filter_is_403(alice, other_project) -> None:
    resp = alice["client"].get(f"/v1/defects?project_id={other_project}")
    assert resp.status_code == 403


def test_empty_membership_returns_empty_page_not_everything(stranger) -> None:
    """最典型的 ABAC 漏洞：成员关系为空时"不加 where"，等于谁都能看全部。"""
    resp = stranger.get("/v1/defects?limit=100")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["has_more"] is False

    queue = stranger.get("/v1/queue?limit=100")
    assert queue.status_code == 200
    assert queue.json()["items"] == []


def test_admin_sees_all_projects(client: TestClient, refs, other_project) -> None:
    """admin 不按项目过滤（运维/审计需要全局视野）。"""
    client.post("/v1/defects", headers={"Idempotency-Key": f"adm-{uuid.uuid4().hex}"},
                json={"title": "管理员提单", "severity": "S3", "project_id": other_project})
    resp = client.get("/v1/defects?limit=100")
    assert resp.status_code == 200
    assert other_project in {i["project_id"] for i in resp.json()["items"]}
