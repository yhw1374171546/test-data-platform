"""业务日期口径的边界测试（纯函数，不需要数据库）。

这一组用例的价值在于**把"差一天"的 bug 钉死在边界上**：
北京时间 00:00 恰好是 UTC 前一天 16:00，任何"看起来对"的实现都会在这里露馅。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.domain.biz_time import (TZ_NAME, biz_date_sql, biz_day_bounds,
                                 ensure_aware, local_now, now, to_biz_date)

UTC = timezone.utc


def test_utc_evening_is_next_day_in_shanghai() -> None:
    """UTC 16:00 = 北京时间次日 00:00 —— 这就是技术方案 §9 坑 #4 的临界点。"""
    assert str(to_biz_date(datetime(2025, 1, 1, 15, 59, 59, tzinfo=UTC))) == "2025-01-01"
    assert str(to_biz_date(datetime(2025, 1, 1, 16, 0, 0, tzinfo=UTC))) == "2025-01-02"


def test_naive_is_treated_as_utc() -> None:
    naive = datetime(2025, 1, 1, 16, 30)              # 没带时区 = 漏了 Z
    aware = naive.replace(tzinfo=UTC)
    assert to_biz_date(naive) == to_biz_date(aware) == date(2025, 1, 2)
    assert ensure_aware(naive).tzinfo == UTC


def test_aware_in_other_zone_is_same_instant() -> None:
    beijing = timezone(timedelta(hours=8))
    as_beijing = datetime(2025, 1, 2, 0, 30, tzinfo=beijing)
    as_utc = datetime(2025, 1, 1, 16, 30, tzinfo=UTC)
    assert as_beijing == as_utc
    assert to_biz_date(as_beijing) == to_biz_date(as_utc) == date(2025, 1, 2)


def test_day_bounds_are_half_open_and_consistent() -> None:
    day = date(2025, 3, 31)
    start, end = biz_day_bounds(day)

    assert start.tzinfo is UTC and end.tzinfo is UTC
    assert end - start == timedelta(hours=24)         # 中国无夏令时
    assert to_biz_date(start) == day, "区间左端点属于当天"
    assert to_biz_date(end - timedelta(microseconds=1)) == day, "右端点前一瞬仍是当天"
    assert to_biz_date(end) == date(2025, 4, 1), "区间右端点是开区间"


def test_biz_date_sql_matches_python_rule() -> None:
    sql = biz_date_sql("executed_at")
    assert "AT TIME ZONE" in sql and TZ_NAME in sql
    assert sql.endswith("::date"), "必须先换算时区再取日期，否则就是 UTC 日"


def test_now_helpers_are_timezone_aware() -> None:
    assert now().tzinfo is not None
    assert local_now().tzinfo is not None
    assert abs((local_now() - now()).total_seconds()) < 1
