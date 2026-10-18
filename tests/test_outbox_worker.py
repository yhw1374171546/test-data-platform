"""outbox worker 测试：**幂等重算、退避、死信、多 worker 并发**（技术方案 §4.4）。

这组用例守住四件事：
  1. 汇总数字对得上明细，且 `skip` 只进 total（表里没有 skipped 列，这是既定口径）
  2. **重复处理不重复计数**——这是"重算 vs 累加"的关键差别
  3. 失败要退避、要累计 attempts、超过上限要 dead；dead 重放后能收敛
  4. 多个 worker 同时跑，同一条消息不能被处理两次

隔离方式：每个测试用一个**独立的业务日**，处理时带 `biz_date=` 过滤。
不加过滤的话，开发库里历史遗留的 pending 消息（演示、冒烟造的）会被一起处理，
断言就变成"看运气"了——隔离不好是测试最贵的坑。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import text

from app.infra.db import session_scope
from app.service.claim import claim_task
from app.service.execution import submit_execution
from app.service.outbox import (diff_summary, outbox_stats, process_batch,
                                replay_dead)


# ────────────────────────── 小工具 ──────────────────────────
def _boom(_row: Any) -> None:
    """故障注入：downstream 挂了。"""
    raise RuntimeError("注入故障：downstream 不可用")


def submit(refs: Dict[str, Any], make_task, idem, result: str,
           executed_at=None) -> int:
    """造一条任务 → 领取 → 提交结果，返回 task_id。"""
    task_id = make_task()
    claim_task(idem_key=idem(), task_id=task_id, tester_id=refs["tester_id"])
    res = submit_execution(idem_key=idem(), task_id=task_id,
                           tester_id=refs["tester_id"], result=result,
                           executed_at=executed_at)
    assert res.code == "recorded", res
    return task_id


def summary_rows(refs: Dict[str, Any]) -> List[Any]:
    """本测试项目下的汇总行（按业务日排序）。"""
    with session_scope() as s:
        return s.execute(text("""
            SELECT biz_date, total, passed, failed, blocked
              FROM daily_exec_summary
             WHERE project_id = :pid ORDER BY biz_date
        """), {"pid": refs["project_id"]}).all()


def messages(refs: Dict[str, Any]) -> List[Any]:
    with session_scope() as s:
        return s.execute(text("""
            SELECT id, status, attempts, next_at, last_error
              FROM outbox WHERE payload ->> 'project_id' = :pid ORDER BY id
        """), {"pid": str(refs["project_id"])}).all()


def force_due(refs: Dict[str, Any]) -> None:
    """把退避中的消息立刻置为到期（测试里不真的等 2^n 秒）。"""
    with session_scope() as s:
        s.execute(text("""
            UPDATE outbox SET next_at = now()
             WHERE payload ->> 'project_id' = :pid
        """), {"pid": str(refs["project_id"])})


@pytest.fixture(autouse=True)
def _no_cache_backlog():
    """清掉"缓存失效"消息的积压。

    这类消息和"汇总"共用一张 outbox 表：缓存一致性压测会留下上万条
    `cache_invalidate` 消息。不清的话 `process_batch(limit=N)` 的配额会被它们吃掉，
    汇总相关用例就"处理不到自己的消息"而随机失败（踩过）。
    测试要隔离的不是"库里有没有别的消息"，而是"我的消息有没有被处理"。
    """
    from app.infra import cache

    with session_scope() as s:
        s.execute(text("""
            DELETE FROM outbox WHERE topic = :t AND status = 'pending'
        """), {"t": cache.TOPIC_CACHE_INVALIDATE})
    yield


# ────────────────────────── 正常汇总 ──────────────────────────
def test_batch_upserts_summary(refs: Dict[str, Any], make_task, idem, biz) -> None:
    for result in ("pass", "pass", "fail", "skip"):
        submit(refs, make_task, idem, result, executed_at=biz.at())

    stats = process_batch(limit=50, biz_date=biz.iso)
    assert stats["done"] == 4 and stats["failed"] == 0 and stats["dead"] == 0

    rows = summary_rows(refs)
    assert len(rows) == 1
    row = rows[0]
    assert (int(row.total), int(row.passed), int(row.failed), int(row.blocked)) == (4, 2, 1, 0)
    # 表里没有 skipped 列：skip 只进 total
    assert int(row.total) - (int(row.passed) + int(row.failed) + int(row.blocked)) == 1

    assert all(m.status == "done" for m in messages(refs))
    assert diff_summary(project_id=refs["project_id"])["diff_count"] == 0


def test_reprocess_does_not_double_count(refs: Dict[str, Any], make_task,
                                         idem, biz) -> None:
    """把已处理的消息重新置为 pending 再跑一次：数字必须不变（重算的幂等性）。"""
    for result in ("pass", "fail"):
        submit(refs, make_task, idem, result, executed_at=biz.at())
    process_batch(limit=50, biz_date=biz.iso)
    before = summary_rows(refs)[0]

    with session_scope() as s:
        s.execute(text("""
            UPDATE outbox SET status = 'pending', attempts = 0, next_at = now()
             WHERE payload ->> 'project_id' = :pid
        """), {"pid": str(refs["project_id"])})

    assert process_batch(limit=50, biz_date=biz.iso)["done"] == 2

    after = summary_rows(refs)[0]
    assert (int(after.total), int(after.passed), int(after.failed)) == \
           (int(before.total), int(before.passed), int(before.failed)), \
           "重算必须幂等；这里翻倍就说明写成了 total = total + 1"


def test_same_day_project_version_only_one_row(refs: Dict[str, Any], make_task,
                                               idem, biz) -> None:
    for _ in range(3):
        submit(refs, make_task, idem, "pass", executed_at=biz.at())
    process_batch(limit=50, biz_date=biz.iso)
    assert len(summary_rows(refs)) == 1, "同一天/项目/版本只能有一行汇总"


# ────────────────────────── 失败、退避、死信 ──────────────────────────
def test_failure_sets_attempts_and_defers(refs: Dict[str, Any], make_task,
                                          idem, biz) -> None:
    submit(refs, make_task, idem, "pass", executed_at=biz.at())

    stats = process_batch(limit=10, biz_date=biz.iso, fault=_boom, backoff_base=30)
    assert stats["failed"] == 1 and stats["dead"] == 0

    msg = messages(refs)[0]
    assert msg.status == "pending", "失败要留在 pending，不能丢"
    assert int(msg.attempts) == 1
    assert "注入故障" in msg.last_error
    assert msg.next_at > datetime.now(timezone.utc), "必须被排到未来（指数退避）"
    assert summary_rows(refs) == [], "失败时不能写汇总"

    # 退避期内再跑：扫不到，说明退避真的生效
    assert process_batch(limit=10, biz_date=biz.iso)["scanned"] == 0

    force_due(refs)
    assert process_batch(limit=10, biz_date=biz.iso)["done"] == 1
    assert int(summary_rows(refs)[0].total) == 1


def test_dead_after_max_attempts_then_replay(refs: Dict[str, Any], make_task,
                                            idem, biz) -> None:
    submit(refs, make_task, idem, "fail", executed_at=biz.at())

    for _ in range(2):
        process_batch(limit=10, biz_date=biz.iso, fault=_boom,
                      max_attempts=2, backoff_base=0)

    msg = messages(refs)[0]
    assert msg.status == "dead", "达到 max_attempts 必须置 dead"
    assert int(msg.attempts) == 2
    assert summary_rows(refs) == []

    # 死信不会自己复活：再跑也不动（必须人工重放）
    assert process_batch(limit=10, biz_date=biz.iso)["scanned"] == 0

    assert replay_dead(ids=[int(msg.id)]) == 1
    assert int(messages(refs)[0].attempts) == 0, "重放要把 attempts 归零"

    assert process_batch(limit=10, biz_date=biz.iso)["done"] == 1
    assert int(summary_rows(refs)[0].total) == 1
    assert diff_summary(project_id=refs["project_id"])["diff_count"] == 0


def test_invalid_message_dies_immediately(refs: Dict[str, Any], biz) -> None:
    """未知 topic / payload 坏掉的消息永远不可能成功 → 当场 dead，别浪费重试。"""
    bad_payload = {"execution_id": 1, "queue_id": 1,
                   "project_id": refs["project_id"],
                   "version_id": refs["version_id"],
                   "result": "pass", "biz_date": biz.iso,
                   "executed_at": biz.at().isoformat()}

    ids = []
    with session_scope() as s:
        for topic, payload in (
            ("bogus_topic", bad_payload),
            ("exec_result_recorded", {k: v for k, v in bad_payload.items()
                                      if k != "biz_date"}),
        ):
            ids.append(int(s.execute(text("""
                INSERT INTO outbox (topic, payload) VALUES (:t, CAST(:p AS JSONB))
                RETURNING id
            """), {"t": topic, "p": json.dumps(payload)}).scalar_one()))

    process_batch(limit=500, max_attempts=5)

    # 断言只针对自己插的两条：process_batch 不按项目过滤，
    # 开发库里可能还有别人的 pending 消息被一起处理（断言 stats 会看运气）。
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT status, attempts, last_error FROM outbox
             WHERE id = ANY(CAST(:ids AS BIGINT[])) ORDER BY id
        """), {"ids": ids}).all()
    assert all(r.status == "dead" for r in rows), "坏消息必须当场 dead"
    assert all(int(r.attempts) == 1 for r in rows), "不该用满 5 次重试预算"
    assert "未知 topic" in rows[0].last_error
    assert "缺字段" in rows[1].last_error


# ────────────────────────── 多 worker 并发 ──────────────────────────
def test_multiple_workers_do_not_double_process(refs: Dict[str, Any], make_task,
                                               idem, biz) -> None:
    n = 20
    for i in range(n):
        submit(refs, make_task, idem, "pass" if i % 2 == 0 else "fail",
               executed_at=biz.at())

    results: List[Dict[str, int]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker() -> None:
        barrier.wait(timeout=30)
        stats = process_batch(limit=n, biz_date=biz.iso)
        with lock:
            results.append(stats)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(r["done"] for r in results) == n, \
        f"5 个 worker 合计应处理 {n} 条，实际 {[r['done'] for r in results]}"

    msgs = messages(refs)
    assert len(msgs) == n
    assert all(m.status == "done" for m in msgs)
    assert all(int(m.attempts) == 1 for m in msgs), \
        "attempts 应全为 1；出现 2 说明同一条消息被两个 worker 各处理了一次"

    row = summary_rows(refs)[0]
    assert int(row.total) == n and int(row.passed) == n // 2
    assert diff_summary(project_id=refs["project_id"])["diff_count"] == 0


# ────────────────────────── 业务日期分组 ──────────────────────────
def test_two_biz_dates_produce_two_rows(refs: Dict[str, Any], make_task,
                                        idem, biz) -> None:
    day2 = biz.plus(1)
    submit(refs, make_task, idem, "pass", executed_at=biz.at())
    submit(refs, make_task, idem, "fail", executed_at=biz.at(11))
    submit(refs, make_task, idem, "pass", executed_at=day2.at())

    process_batch(limit=50, biz_date=biz.iso)
    process_batch(limit=50, biz_date=day2.iso)

    rows = summary_rows(refs)
    assert [(str(r.biz_date), int(r.total), int(r.passed), int(r.failed))
            for r in rows] == [(biz.iso, 2, 1, 1), (day2.iso, 1, 1, 0)]


def test_biz_date_filter_processes_only_that_day(refs: Dict[str, Any], make_task,
                                                 idem, biz) -> None:
    day2 = biz.plus(1)
    submit(refs, make_task, idem, "pass", executed_at=biz.at())
    submit(refs, make_task, idem, "pass", executed_at=day2.at())

    assert process_batch(limit=50, biz_date=biz.iso)["done"] == 1
    assert [str(r.biz_date) for r in summary_rows(refs)] == [biz.iso], \
        "只处理指定那一天的消息"

    process_batch(limit=50)
    assert sorted(str(r.biz_date) for r in summary_rows(refs)) == \
           sorted([biz.iso, day2.iso])


def test_stats_and_list_messages(refs: Dict[str, Any], make_task, idem, biz) -> None:
    submit(refs, make_task, idem, "pass", executed_at=biz.at())
    stats = outbox_stats()
    assert stats["pending"] >= 1 and stats["done"] >= 0

    process_batch(limit=50, biz_date=biz.iso)
    stats_after = outbox_stats()
    assert stats_after["summary_rows"] >= 1
