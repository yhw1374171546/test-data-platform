"""生成 Grafana 面板 JSON（写到 deploy/grafana/dashboards/tdp.json）。

为什么用脚本生成而不是手写 JSON：
面板里 `id` / `gridPos` 是重复且容易写错的字段；手写 300 行 JSON 出错后
Grafana 只会静默不加载，很难查。生成 + 校验（id 唯一、坐标不重叠）更可靠。
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "tdp.json"
DS = {"type": "prometheus", "uid": "prometheus"}

PANELS = [
    # (标题, 表达式, 类型, 宽, 高, 单位, 图例)
    ("请求速率（按状态）", "sum by (status) (rate(tdp_http_requests_total[5m]))",
     "timeseries", 12, 7, "reqps", "{{status}}"),
    ("延迟 P95 / P99", "histogram_quantile(0.95, sum by (le) (rate(tdp_http_request_duration_seconds_bucket[5m])))",
     "timeseries", 12, 7, "s", "p95"),
    ("在途请求", "tdp_http_inflight", "stat", 6, 5, "short", "inflight"),
    ("outbox 积压", "tdp_outbox_pending", "stat", 6, 5, "short", "pending"),
    ("死信（应恒为 0）", "tdp_outbox_dead", "stat", 6, 5, "short", "dead"),
    ("对账差异（按 kind）", "sum by (kind) (tdp_recon_discrepancies)",
     "timeseries", 6, 5, "short", "{{kind}}"),
    ("预约结果分布", "sum by (result) (rate(tdp_booking_attempts_total[5m]))",
     "timeseries", 12, 6, "reqps", "{{result}}"),
    ("任务领取分布", "sum by (result) (rate(tdp_task_claim_attempts_total[5m]))",
     "timeseries", 12, 6, "reqps", "{{result}}"),
    ("缓存命中率", "sum(rate(tdp_cache_hits_total[10m])) / clamp_min(sum(rate(tdp_cache_hits_total[10m])) + sum(rate(tdp_cache_misses_total[10m])), 0.001)",
     "timeseries", 8, 6, "percentunit", "hit rate"),
    ("缓存失效率（为 0 说明写路径忘了失效）",
     "sum by (reason) (rate(tdp_cache_invalidations_total[5m]))",
     "timeseries", 8, 6, "reqps", "{{reason}}"),
    ("认证失败 / 限流", "sum by (result) (rate(tdp_auth_attempts_total{result!=\"ok\"}[5m]))",
     "timeseries", 8, 6, "reqps", "{{result}}"),
    ("被限流的请求", "sum by (route) (rate(tdp_rate_limited_total[5m]))",
     "timeseries", 12, 6, "reqps", "{{route}}"),
    ("执行提交分布", "sum by (result) (rate(tdp_execution_submits_total[5m]))",
     "timeseries", 12, 6, "reqps", "{{result}}"),
]


def build() -> dict:
    panels = []
    x = y = 0
    row_width = 24
    for idx, (title, expr, kind, width, height, unit, legend) in enumerate(PANELS, start=1):
        if x + width > row_width:
            x, y = 0, y + 7                 # 换行（行高按最大 7 估，够用）
        panel = {
            "id": idx,
            "type": kind,
            "title": title,
            "datasource": DS,
            "gridPos": {"h": height, "w": width, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
            "targets": [{"refId": "A", "expr": expr, "legendFormat": legend,
                         "datasource": DS}],
        }
        panels.append(panel)
        x += width
    return {
        "title": "测试数据管理平台 · 总览",
        "uid": "tdp-overview",
        "tags": ["tdp"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "editable": True,
        "panels": panels,
    }


if __name__ == "__main__":
    dash = build()
    assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"]), "面板 id 必须唯一"
    for p in dash["panels"]:
        assert p["targets"] and p["targets"][0]["expr"], p["title"]
    OUT.write_text(json.dumps(dash, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} · panels={len(dash['panels'])}")
