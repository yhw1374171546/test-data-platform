"""对账规则的纯函数测试（不需要数据库）。

这组用例的价值在于**误报**：一个对的日期必须一条差异都不报。
对账系统最常见的死法不是"漏检"，而是"每天报一堆假差异，两周后没人看了"。
"""
from __future__ import annotations

from datetime import date

from app.domain.recon_rules import (KIND_EXTRA_SUMMARY, KIND_FAIL_WITHOUT_DEFECT,
                                    KIND_MISSING_SUMMARY, KIND_SUMMARY_MISMATCH,
                                    reconcile)

DAY = date(2025, 5, 1)


def _row(total: int, passed: int, failed: int, blocked: int, *,
         day: date = DAY, pid: int = 1, vid: int = 1) -> dict:
    return {"biz_date": day, "project_id": pid, "version_id": vid,
            "total": total, "passed": passed, "failed": failed, "blocked": blocked}


def _kinds(result: dict) -> list:
    return sorted(d.kind for d in result.get((DAY, 1), []))


# ────────────────────────── 不误报 ──────────────────────────
def test_consistent_data_has_no_discrepancy() -> None:
    detail = [_row(10, 6, 3, 1)]
    summary = [_row(10, 6, 3, 1)]
    assert reconcile(detail=detail, summary=summary, fails=[]) == {}


def test_skip_only_in_total_is_not_a_discrepancy() -> None:
    """total 含 skip，passed+failed+blocked < total 是正常的——这是最容易写成误报的地方。"""
    detail = [_row(5, 2, 1, 1)]      # 5 = 2 pass + 1 fail + 1 blocked + 1 skip
    summary = [_row(5, 2, 1, 1)]
    assert reconcile(detail=detail, summary=summary, fails=[]) == {}


def test_multiple_versions_all_consistent() -> None:
    detail = [_row(4, 2, 1, 1, vid=1), _row(6, 4, 1, 1, vid=2)]
    summary = [_row(4, 2, 1, 1, vid=1), _row(6, 4, 1, 1, vid=2)]
    assert reconcile(detail=detail, summary=summary, fails=[]) == {}


def test_other_days_are_independent() -> None:
    """没有差异的那一天不产生任何条目——只返回"有差异"的 (日, 项目)。"""
    day2 = date(2025, 5, 2)
    result = reconcile(detail=[_row(1, 1, 0, 0), _row(1, 0, 1, 0, day=day2)],
                       summary=[_row(1, 1, 0, 0)],
                       fails=[])
    assert sorted(result) == [(day2, 1)]
    assert [d.kind for d in result[(day2, 1)]] == [KIND_MISSING_SUMMARY]


# ────────────────────────── 四类差异 ──────────────────────────
def test_missing_summary() -> None:
    result = reconcile(detail=[_row(7, 5, 2, 0)], summary=[], fails=[])
    assert _kinds(result) == [KIND_MISSING_SUMMARY]
    d = result[(DAY, 1)][0]
    assert (d.expected, d.actual) == (7, 0)


def test_extra_summary() -> None:
    result = reconcile(detail=[], summary=[_row(3, 3, 0, 0)], fails=[])
    assert _kinds(result) == [KIND_EXTRA_SUMMARY]
    d = result[(DAY, 1)][0]
    assert (d.expected, d.actual) == (0, 3)


def test_summary_mismatch_reports_which_field() -> None:
    result = reconcile(detail=[_row(10, 6, 3, 1)],
                       summary=[_row(10, 7, 2, 1)], fails=[])
    assert _kinds(result) == [KIND_SUMMARY_MISMATCH]
    d = result[(DAY, 1)][0]
    assert d.expected == 10 and d.actual == 10, "total 一样，但列内数字不同"
    fields = d.detail["fields"][0]
    assert fields["diff"] == {"passed": 1, "failed": -1}, "要指出具体哪几列差了多少"


def test_fail_without_defect_lists_executions() -> None:
    fails = [{"biz_date": DAY, "project_id": 1, "execution_id": 101},
             {"biz_date": DAY, "project_id": 1, "execution_id": 102}]
    result = reconcile(detail=[_row(2, 0, 2, 0)], summary=[_row(2, 0, 2, 0)],
                       fails=fails)
    assert _kinds(result) == [KIND_FAIL_WITHOUT_DEFECT]
    d = result[(DAY, 1)][0]
    assert d.expected == 2 and d.actual == 0
    assert d.detail["execution_ids"] == [101, 102]


def test_all_four_kinds_at_once() -> None:
    """同一天同时存在 4 类问题：四类都要报出来，且互不覆盖。"""
    result = reconcile(
        detail=[_row(4, 2, 1, 1, vid=1),      # v1 有汇总且对得上
                _row(2, 1, 1, 0, vid=2),      # v2 没汇总 → missing
                _row(3, 1, 1, 1, vid=3)],     # v3 汇总数字不对 → mismatch
        summary=[_row(4, 2, 1, 1, vid=1),
                 _row(3, 2, 0, 1, vid=3),     # 少算 1 个 fail
                 _row(5, 5, 0, 0, vid=4)],    # v4 没有明细 → extra
        fails=[{"biz_date": DAY, "project_id": 1, "execution_id": 201}],
    )
    assert _kinds(result) == sorted([KIND_MISSING_SUMMARY, KIND_EXTRA_SUMMARY,
                                     KIND_SUMMARY_MISMATCH, KIND_FAIL_WITHOUT_DEFECT])
    missing = [d for d in result[(DAY, 1)] if d.kind == KIND_MISSING_SUMMARY][0]
    assert missing.expected == 2, "只统计缺汇总的那个版本"
    extra = [d for d in result[(DAY, 1)] if d.kind == KIND_EXTRA_SUMMARY][0]
    assert extra.actual == 5


def test_projects_are_split() -> None:
    result = reconcile(detail=[_row(1, 1, 0, 0, pid=1), _row(1, 1, 0, 0, pid=2)],
                       summary=[_row(1, 1, 0, 0, pid=1)], fails=[])
    assert sorted(result) == [(DAY, 2)]
