"""前端配套接口的契约测试：下拉字典 + 日报看板。

这两个接口是给页面用的，所以重点测"前端拿到的东西能不能直接用"：
· 字典要一次给全 4 类下拉，且字段名和页面上的 label 对得上；
· 日报要能按日期取、能按项目过滤，合计要跟明细自洽。
"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient



def test_options_returns_all_four_dropdowns(client: TestClient) -> None:
    resp = client.get("/v1/meta/options")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"projects", "versions", "vehicles", "testers"}
    for key in data:
        assert isinstance(data[key], list) and data[key], f"{key} 不该是空的（演示库里有数据）"
    # 字段名要和页面上的展示字段一致，否则页面会出现空白列
    assert {"id", "code", "name", "model"} <= set(data["projects"][0])
    assert {"id", "vin", "model"} <= set(data["vehicles"][0])
    assert {"id", "employee_no", "name"} <= set(data["testers"][0])


def test_options_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/v1/meta/options").status_code == 401


def test_daily_report_is_consistent_and_filterable(client: TestClient) -> None:
    day = "2026-10-18"
    whole = client.get("/v1/reports/daily", params={"biz_date": day}).json()
    assert whole["biz_date"] == day
    # 合计 = 明细逐项相加（页面上的四张卡片直接用 totals）
    for key in ("total", "passed", "failed", "blocked"):
        assert whole["totals"][key] == sum(int(i[key] or 0) for i in whole["items"])
    assert whole["totals"]["skipped"] == (
        whole["totals"]["total"] - whole["totals"]["passed"]
        - whole["totals"]["failed"] - whole["totals"]["blocked"]
    )

    if whole["items"]:
        pid = whole["items"][0]["project_id"]
        one = client.get("/v1/reports/daily", params={"biz_date": day, "project_id": pid}).json()
        assert {i["project_id"] for i in one["items"]} == {pid}
        assert len(one["items"]) <= len(whole["items"])


def test_daily_report_rejects_bad_date(client: TestClient) -> None:
    resp = client.get("/v1/reports/daily", params={"biz_date": "2026/10/18"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_argument"


def test_ui_is_served_when_built(client: TestClient) -> None:
    """构建产物存在时，/ui/ 要能直接打开（否则面试演示会一脸空白）。"""
    dist = __import__("pathlib").Path(__file__).resolve().parent.parent / "web" / "dist" / "index.html"
    if not dist.exists():
        pytest.skip("前端未构建：先跑 dev.cmd web")
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert 'id="app"' in resp.text
