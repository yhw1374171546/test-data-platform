"""调度器测试：分布式互斥 + 调度算术 + 三个任务。

最值得测的不是"任务能跑"，而是**多副本下只跑一次**：
定时任务最典型的线上事故就是 5 个副本同时对账/同时清理，把库压垮或者互相打架。
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.domain.biz_time import TZ
from app.infra.db import session_scope

BASE = Path(__file__).resolve().parent.parent


def _load_scheduler():
    """按文件加载 scripts/scheduler.py（它不是包，测试里直接 import 会污染 sys.path）。"""
    spec = importlib.util.spec_from_file_location("tdp_scheduler", BASE / "scripts" / "scheduler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tdp_scheduler"] = module
    spec.loader.exec_module(module)
    return module


scheduler = _load_scheduler()


# ────────────────────────── 分布式互斥 ──────────────────────────
def test_advisory_lock_is_exclusive_across_sessions() -> None:
    name = f"test-lock-{uuid.uuid4().hex[:8]}"
    with scheduler.advisory_lock(name) as first:
        assert first is True
        # 另一个连接 = 另一个会话：拿不到同一把锁
        with scheduler.advisory_lock(name) as second:
            assert second is False, "同一把锁被两个会话同时拿到，多副本会重复执行"
    # 释放后可以再拿
    with scheduler.advisory_lock(name) as again:
        assert again is True


def test_run_job_skips_when_lock_held(monkeypatch) -> None:
    """别的副本在跑时，本副本必须**跳过**（返回 None），而不是排队等。"""
    name = "dead_letter_check"
    holder = scheduler.advisory_lock(f"tdp:scheduler:{name}")
    holder.__enter__()
    try:
        called = {"n": 0}
        monkeypatch.setitem(scheduler.JOBS, name, lambda: called.__setitem__("n", 1))
        assert scheduler.run_job(name) is None
        assert called["n"] == 0
    finally:
        holder.__exit__(None, None, None)


# ────────────────────────── 调度算术（纯函数，不睡眠）──────────────────────────
def test_interval_schedule_fires_immediately_then_waits() -> None:
    schedule = scheduler.IntervalSchedule(60)
    assert schedule.is_due(0.0) is True
    schedule.advance(0.0)
    assert schedule.is_due(30.0) is False
    assert schedule.is_due(60.0) is True


def test_daily_schedule_fires_once_per_day_after_target_time() -> None:
    schedule = scheduler.DailySchedule("00:10")
    early = datetime(2026, 3, 2, 0, 5, tzinfo=TZ)
    late = datetime(2026, 3, 2, 0, 30, tzinfo=TZ)
    assert schedule.is_due(early) is False
    assert schedule.is_due(late) is True
    schedule.advance(late)
    assert schedule.is_due(late) is False, "同一天不能跑第二次"
    assert schedule.is_due(datetime(2026, 3, 3, 0, 30, tzinfo=TZ)) is True


# ────────────────────────── 任务本身 ──────────────────────────
def test_cleanup_removes_only_expired_keys() -> None:
    old_key = f"sched-old-{uuid.uuid4().hex}"
    new_key = f"sched-new-{uuid.uuid4().hex}"
    with session_scope() as s:
        for key, days in ((old_key, 30), (new_key, 0)):
            s.execute(text("""
                INSERT INTO idempotency_keys (scope, key, body_hash, status_code, response, created_at)
                VALUES ('sched-test', :k, 'h', 200, '{}'::jsonb, :ts)
            """), {"k": key, "ts": datetime.now(timezone.utc) - timedelta(days=days)})

    deleted = scheduler.cleanup_idempotency(ttl_days=7)
    assert deleted >= 1

    with session_scope() as s:
        left = {r.key for r in s.execute(text(
            "SELECT key FROM idempotency_keys WHERE key IN (:a, :b)"),
            {"a": old_key, "b": new_key}).all()}
    assert old_key not in left, "过期的键必须删掉"
    assert new_key in left, "没过期的键不能删"


def test_dead_letter_check_returns_count_and_updates_gauge() -> None:
    dead = scheduler.check_dead_letters()
    assert isinstance(dead, int) and dead >= 0


def test_run_all_executes_every_job() -> None:
    results = scheduler.run_all(ttl_days=7)
    assert set(results) == set(scheduler.JOBS)
    recon = results["recon_daily"]
    assert recon is None or "pairs_checked" in recon
