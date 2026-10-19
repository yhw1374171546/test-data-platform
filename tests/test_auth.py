"""认证与授权测试：token 生命周期 + 角色边界 + 失败不泄露信息。

为什么这些用例值得写：
认证最容易出的事故不是"没认证"，而是**认证了但边界错了**——
比如 viewer 能调管理员接口、过期 token 还能用、登录失败返回
"用户不存在"让人撞库。这三类都在下面钉住。
"""
from __future__ import annotations

from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.infra import auth
from app.infra.db import session_scope
from app.service import users


@pytest.fixture()
def account(refs: Dict[str, Any]) -> Iterator[Dict[str, str]]:
    """造一个真实账号（口令哈希走 PBKDF2，和 init_db 灌演示数据同一条路径）。

    用完要删：`app_users` 没有外键级联，留着会在库里堆一堆 `u-xxx`（第一次就堆到 59 个）。
    """
    name = f"u-{refs['tag']}"
    users.create_user(name, "Secret@12345", "admin")
    yield {"username": name, "password": "Secret@12345"}
    with session_scope() as s:
        s.execute(text("DELETE FROM app_users WHERE username = :u"), {"u": name})


def _login(client: TestClient, username: str, password: str):
    return client.post("/v1/auth/token", json={"username": username, "password": password})


# ────────────────────────── 口令哈希（纯函数）──────────────────────────
def test_password_hash_is_salted_and_verifiable() -> None:
    first = auth.hash_password("Same@Password")
    second = auth.hash_password("Same@Password")
    assert first != second, "每次都要新盐，否则相同口令的哈希一样，等于泄露'这两个人密码相同'"
    assert auth.verify_password("Same@Password", first)
    assert not auth.verify_password("Wrong@Password", first)


def test_password_verify_rejects_malformed_hash() -> None:
    for bad in ("", "plain-text", "bcrypt$10$xx", "pbkdf2_sha256$notanint$a$b"):
        assert auth.verify_password("whatever", bad) is False


def test_role_hierarchy() -> None:
    assert auth.allows("admin", "tester")
    assert auth.allows("tester", "tester")
    assert not auth.allows("viewer", "tester")
    assert not auth.allows("unknown", "viewer")


# ────────────────────────── 登录 ──────────────────────────
def test_login_issues_working_token(client: TestClient, account: Dict[str, str]) -> None:
    resp = _login(client, account["username"], account["password"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer" and body["role"] == "admin"
    assert body["expires_in"] > 0

    me = client.get("/v1/auth/me",
                    headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == account["username"]


def test_login_failures_are_indistinguishable(client: TestClient,
                                              account: Dict[str, str]) -> None:
    """用户不存在 / 口令错，必须**返回同一个响应**，否则接口就是用户名枚举器。"""
    wrong = _login(client, account["username"], "not-the-password")
    unknown = _login(client, "nobody-here", "whatever")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == unknown.json()["error"] == "invalid_credentials"
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_inactive_account_cannot_login(client: TestClient, account: Dict[str, str]) -> None:
    users.set_active(account["username"], False)
    resp = _login(client, account["username"], account["password"])
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_credentials"


# ────────────────────────── token 校验 ──────────────────────────
def test_missing_token_is_401(anon_client: TestClient) -> None:
    resp = anon_client.get("/v1/defects")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized_missing"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_garbage_token_is_401(anon_client: TestClient) -> None:
    resp = anon_client.get("/v1/defects", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized_invalid"


def test_expired_token_is_401(anon_client: TestClient) -> None:
    token, _ = auth.issue_token("someone", "admin", ttl_seconds=-10)   # 签发即过期
    resp = anon_client.get("/v1/defects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized_expired"


def test_token_signed_with_other_secret_is_rejected(anon_client: TestClient,
                                                    monkeypatch) -> None:
    token, _ = auth.issue_token("someone", "admin")
    # 换个密钥再验：签名对不上，必须拒
    monkeypatch.setenv("TDP_JWT_SECRET", "another-secret")
    resp = anon_client.get("/v1/defects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized_invalid"


# ────────────────────────── 角色边界 ──────────────────────────
def test_viewer_cannot_write(viewer_client: TestClient, refs: Dict[str, Any]) -> None:
    resp = viewer_client.post("/v1/queue", headers={"Idempotency-Key": f"viewer-{refs['tag']}"},
                              json={"project_id": refs["project_id"],
                                    "version_id": refs["version_id"],
                                    "case_no": f"TC-VIEW-{refs['tag']}"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"
    assert resp.json()["detail"]["need"] == "tester"


def test_viewer_can_read(viewer_client: TestClient) -> None:
    assert viewer_client.get("/v1/defects").status_code == 200
    assert viewer_client.get("/v1/queue").status_code == 200


def test_tester_cannot_touch_admin_endpoints(tester_client: TestClient) -> None:
    assert tester_client.get("/v1/admin/outbox").status_code == 403
    assert tester_client.post("/v1/jobs/summary",
                              headers={"Idempotency-Key": "tester-summary"}
                              ).status_code == 403


def test_admin_can_read_admin_endpoints(client: TestClient) -> None:
    assert client.get("/v1/admin/outbox").status_code == 200


# ────────────────────────── 应急开关 ──────────────────────────
def test_auth_off_allows_anonymous(anon_client: TestClient, monkeypatch) -> None:
    """`TDP_AUTH=off` 是内网联调的应急开关，必须真的能关掉，且默认是开的。"""
    assert auth.auth_enabled() is True
    monkeypatch.setenv("TDP_AUTH", "off")
    assert auth.auth_enabled() is False
    assert anon_client.get("/v1/defects").status_code == 200
    assert anon_client.get("/v1/admin/outbox").status_code == 200


def test_auth_metrics_are_recorded(anon_client: TestClient, account: Dict[str, str]) -> None:
    anon_client.get("/v1/defects")                      # 401 一次
    metric = anon_client.get("/metrics").text
    assert "tdp_auth_attempts_total" in metric
