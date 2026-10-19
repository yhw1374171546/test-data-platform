"""演示账号与上线自检的单元测试。

两个容易出事的点：
1. 线上演示账号必须是 **viewer**（只读）—— 给 admin 等于把库交给公网；
2. prod_check 必须在默认值没改时**报 FAIL**，否则它就是个摆设。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"tdp_{name}", BASE / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"tdp_{name}"] = module
    spec.loader.exec_module(module)
    return module


init_db = _load("init_db")
prod_check = _load("prod_check")


def test_demo_users_defaults_have_three_roles() -> None:
    roles = {role for _u, _p, role in init_db.demo_users()}
    assert roles == {"admin", "tester", "viewer"}


def test_demo_user_from_env_is_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDP_DEMO_USER", "guest")
    monkeypatch.setenv("TDP_DEMO_PASSWORD", "Guest@12345")
    users = {u: r for u, _p, r in init_db.demo_users()}
    assert users["guest"] == "viewer", "线上演示账号必须是只读角色"
    assert len(init_db.demo_users()) == 4


def test_admin_password_can_be_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """线上用环境变量覆盖 admin 初始口令：不依赖"上线后记得改"。"""
    monkeypatch.setenv("TDP_ADMIN_PASSWORD", "Str0ng-Admin-Pass-123")
    users = {u: p for u, p, _r in init_db.demo_users()}
    assert users["admin"] == "Str0ng-Admin-Pass-123"
    assert users["tester"] == "Tester@12345", "只覆盖 admin，别动其它演示账号"


def test_demo_user_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDP_DEMO_USER", "guest")
    monkeypatch.delenv("TDP_DEMO_PASSWORD", raising=False)
    assert "guest" not in {u for u, _p, _r in init_db.demo_users()}


def test_prod_check_flags_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TDP_JWT_SECRET", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("TDP_RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("TDP_AUTH", "off")
    results = {name: ok for name, ok, _detail in prod_check.check_env()}
    assert results["JWT 密钥"] is False
    assert results["数据库口令"] is False
    assert results["限流后端"] is False
    assert results["认证开关"] is False


def test_prod_check_passes_with_strong_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDP_JWT_SECRET", "z" * 48)
    monkeypatch.setenv("POSTGRES_PASSWORD", "S" * 24)
    monkeypatch.setenv("TDP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("TDP_AUTH", "on")
    monkeypatch.delenv("TDP_DEMO_USER", raising=False)
    failed = [name for name, ok, _d in prod_check.check_env() if not ok]
    assert failed == [], f"强配置不该有 FAIL：{failed}"
