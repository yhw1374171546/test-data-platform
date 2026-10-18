"""占用方案 P99 对比：条件更新(A) / 悲观锁(B) / Redis 预占(C)（技术方案 §4.1）。

三组都是**同一套业务代码**（`create_booking(scheme=...)`：同样的幂等键、同样的建单、
同样的档期回填），只换"怎么抢那一行"：

    A conditional      一条 UPDATE ... WHERE status='free'，affected rows 定胜负
    B pessimistic      事务内 SELECT ... FOR UPDATE，再判断/更新
    C redis            SETNX 预占，抢不到直接失败（一次 DB 都不打），抢到的才写库

除了 P99，还记录**每个方案打到数据库的语句数**（用 `pg_stat_database.xact_commit` 差值近似）——
C 的价值不在于"赢的人更快"，而在于"输的人根本不碰数据库"。

用法：
    python bench/slot_schemes.py --concurrency 200
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, text                           # noqa: E402

from app.infra import redis_lock                             # noqa: E402
from app.infra.db import engine, session_scope               # noqa: E402
from app.service.booking import SlotNotAvailable, create_booking  # noqa: E402

# 精确统计"打到数据库的 SQL 语句数"。
# 为什么不用 pg_stat_database.xact_commit 的差值：那张视图是异步刷新的，
# 事务刚结束时不一定会更新 —— 第一版测出 C 方案"0 次 DB 事务"，明显是假的。
_STATEMENTS = {"count": 0}
_STATEMENTS_LOCK = threading.Lock()


def _count_statement(*_args: Any, **_kwargs: Any) -> None:
    with _STATEMENTS_LOCK:
        _STATEMENTS["count"] += 1


event.listen(engine, "before_cursor_execute", _count_statement)


def seed(slot_date: str, half: int = 0) -> Dict[str, int]:
    """一台车 + 一个 free 档期 + 一个测试员。每轮实验都用新车辆，避免互相干扰。"""
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, 'P99 实验项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-P99-{tag}"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO vehicles (vin, model) VALUES (:v, 'U8') RETURNING id
        """), {"v": f"VINP99{tag.upper()}"}).scalar_one()
        tid = s.execute(text("""
            INSERT INTO testers (employee_no, name) VALUES (:e, 'P99 测试员') RETURNING id
        """), {"e": f"EP99{tag}"}).scalar_one()
        s.execute(text("""
            INSERT INTO vehicle_slots (vehicle_id, slot_date, half, status)
            VALUES (:v, :d, :h, 'free')
        """), {"v": vid, "d": slot_date, "h": half})
    return {"project_id": int(pid), "vehicle_id": int(vid), "tester_id": int(tid)}


def statement_count() -> int:
    with _STATEMENTS_LOCK:
        return int(_STATEMENTS["count"])


def run_race(scheme: str, env: Dict[str, int], slot_date: str, half: int,
             concurrency: int) -> Dict[str, Any]:
    latencies: List[float] = []
    codes: Counter = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency, timeout=60)

    def worker() -> None:
        key = f"bench-{uuid.uuid4().hex}"
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        started = time.perf_counter()
        try:
            create_booking(idem_key=key, project_id=env["project_id"],
                           vehicle_id=env["vehicle_id"], testers_id=env["tester_id"],
                           slot_date=slot_date, half=half, scheme=scheme)
            tag = "created"
        except SlotNotAvailable:
            tag = "slot_taken"
        except Exception as exc:                    # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        elapsed = time.perf_counter() - started
        with lock:
            codes[tag] += 1
            latencies.append(elapsed)

    statements_before = statement_count()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statements = statement_count() - statements_before

    latencies.sort()
    p = lambda q: latencies[min(len(latencies) - 1, int(len(latencies) * q))] * 1000
    return {
        "scheme": scheme,
        "codes": dict(codes),
        "created": codes.get("created", 0),
        "db_tx": statements,
        "p50": p(0.50), "p95": p(0.95), "p99": p(0.99), "max": latencies[-1] * 1000,
        "errors": sum(v for k, v in codes.items() if k.startswith("error:")),
    }


def median_of(rounds: List[Dict[str, Any]], key: str) -> float:
    """多轮结果取中位数（P99 单轮抖动大，中位数才有代表性）。"""
    return statistics.median(x[key] for x in rounds)


def booked_count(vehicle_id: int, slot_date: str, half: int) -> int:
    with session_scope() as s:
        return int(s.execute(text("""
            SELECT count(*) FROM bookings
             WHERE vehicle_id = :v AND slot_date = :d AND half = :h AND status = 'active'
        """), {"v": vehicle_id, "d": slot_date, "h": half}).scalar_one())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=200,
                    help="并发线程数（技术方案写 1000 做正确性；P99 对比用 200，避免 CPU 饱和成噪声）")
    ap.add_argument("--rounds", type=int, default=3, help="每个方案跑几轮取中位数")
    args = ap.parse_args()

    print("=" * 78)
    print(f"占用方案 P99 对比 · 并发 {args.concurrency} · 每方案 {args.rounds} 轮")
    print("=" * 78)
    print("三个方案共用同一套业务代码（幂等 + 建单 + 回填），只换抢占手段\n")

    results: Dict[str, List[Dict[str, Any]]] = {}
    base = date(2031, 1, 1)

    try:
        for idx, scheme in enumerate(("conditional", "pessimistic", "redis")):
            label = {"conditional": "A 条件更新", "pessimistic": "B 悲观锁",
                     "redis": "C Redis 预占"}[scheme]
            rounds: List[Dict[str, Any]] = []
            for r in range(args.rounds):
                slot_date = str(base + timedelta(days=idx * 10 + r))
                env = seed(slot_date)
                if scheme == "redis":
                    redis_lock.release_all_for_vehicle(env["vehicle_id"])
                rounds.append(run_race(scheme, env, slot_date, 0, args.concurrency))
                booked = booked_count(env["vehicle_id"], slot_date, 0)
                if rounds[-1]["created"] != 1 or booked != 1:
                    print(f"  [FAIL] {scheme} 第 {r + 1} 轮出现超占："
                          f"created={rounds[-1]['created']} booked={booked}")
                    return 1
            results[scheme] = rounds
            print(f"[{label}]")
            print(f"      结果分布(首轮)：{rounds[0]['codes']}")
            print(f"      P50 {median_of(rounds, 'p50'):>7.2f}ms   "
                  f"P95 {median_of(rounds, 'p95'):>7.2f}ms   "
                  f"P99 {median_of(rounds, 'p99'):>7.2f}ms   "
                  f"max {median_of(rounds, 'max'):>7.2f}ms")
            print(f"      打到 DB 的 SQL 语句数（首轮）：{rounds[0]['db_tx']}"
                  f"  （成功 {rounds[0]['created']} / 失败 {args.concurrency - rounds[0]['created']}）")
            print()

        print("=" * 78)
        print("对比（中位数）")
        print("=" * 78)
        print(f"  {'方案':<14}{'P50':>9}{'P95':>9}{'P99':>9}{'max':>9}{'DB 语句':>10}")
        for scheme, rounds in results.items():
            label = {"conditional": "A 条件更新", "pessimistic": "B 悲观锁",
                     "redis": "C Redis 预占"}[scheme]
            print(f"  {label:<14}{median_of(rounds, 'p50'):>8.2f}ms"
                  f"{median_of(rounds, 'p95'):>8.2f}ms"
                  f"{median_of(rounds, 'p99'):>8.2f}ms"
                  f"{median_of(rounds, 'max'):>8.2f}ms"
                  f"{median_of(rounds, 'db_tx'):>10.0f}")

        a = results["conditional"]
        c = results["redis"]
        print("\n  读法：")
        print("    · 三者都必须「成功恰好 1」——这是正确性，不是性能。三轮都断言过了。")
        print("    · B 多一次 SELECT ... FOR UPDATE，P99 通常比 A 高；单档期场景没有收益，")
        print("      它的价值在「连续占多半天 + 先查后改」这类复杂逻辑。")
        print("    · C 的赢家并不更快（还是要写库），但**输家一次 DB 都不打**，")
        print("      所以高并发下 DB 压力与 P99 都明显更低 —— 它的定位是抢购式峰值。")
        print(f"    本轮 DB 语句数：A {statistics.median(x['db_tx'] for x in a):.0f} / "
              f"C {statistics.median(x['db_tx'] for x in c):.0f}")
    finally:
        # 清掉 C 方案留下的预占 key，别影响后续实验
        with session_scope() as s:
            vids = s.execute(text(
                "SELECT id FROM vehicles WHERE vin LIKE 'VINP99%'")).scalars().all()
        for vid in vids:
            redis_lock.release_all_for_vehicle(int(vid))

    print("\n" + "=" * 78)
    print("  结论：三方案正确性一致（恰好一人成功）；P99 与 DB 压力见上表")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
