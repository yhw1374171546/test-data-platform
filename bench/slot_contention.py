"""档期竞争压测：验证「原子占用 + 幂等」在真并发下的正确性。

三组实验，每组都要出数：
  A. 不同幂等键抢同一档期  -> 成功恰好 1，重复占用 0
  B. 相同幂等键并发        -> 恰好 1 次 created，其余 replayed（幂等生效）
  C. 同 key 换 body        -> 422，且不产生任何占用

用法：
    python bench/slot_contention.py --concurrency 1000
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import uuid
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                                  # noqa: E402

from app.infra.db import session_scope                       # noqa: E402
from app.service.booking import (SlotNotAvailable,            # noqa: E402
                                 create_booking)


def seed(slot_date: str, half: int) -> tuple:
    """准备一台车 + 一个 free 档期（每轮实验用新日期，避免互相干扰）。"""
    with session_scope() as s:
        pid = s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, :n, :m)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
        """), {"c": f"BENCH-{uuid.uuid4().hex[:8]}", "n": "压测项目", "m": "U8"}).scalar_one()
        vid = s.execute(text("""
            INSERT INTO vehicles (vin, model) VALUES (:vin, :m)
            RETURNING id
        """), {"vin": f"VIN{uuid.uuid4().hex[:14].upper()}", "m": "U8"}).scalar_one()
        tid = s.execute(text("""
            INSERT INTO testers (employee_no, name) VALUES (:e, :n) RETURNING id
        """), {"e": f"T{uuid.uuid4().hex[:8]}", "n": "压测员"}).scalar_one()
        s.execute(text("""
            INSERT INTO vehicle_slots (vehicle_id, slot_date, half, status)
            VALUES (:v, :d, :h, 'free')
        """), {"v": vid, "d": slot_date, "h": half})
    return pid, vid, tid


def run_race(concurrency: int, pid: int, vid: int, tid: int,
             slot_date: str, half: int, same_key: bool) -> Counter:
    """并发预约。same_key=True 时所有线程共用一个幂等键。"""
    results: Counter = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency)          # 让所有线程尽量同时开抢
    shared_key = f"idem-{uuid.uuid4().hex}"

    def worker(i: int) -> None:
        key = shared_key if same_key else f"idem-{uuid.uuid4().hex}"
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        try:
            res = create_booking(idem_key=key, project_id=pid, vehicle_id=vid,
                                 testers_id=tid, slot_date=slot_date, half=half)
            tag = res.code
        except SlotNotAvailable:
            tag = "slot_taken"
        except Exception as exc:                       # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        with lock:
            results[tag] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def count_booked(vid: int, slot_date: str, half: int) -> int:
    with session_scope() as s:
        return int(s.execute(text("""
            SELECT count(*) FROM bookings
             WHERE vehicle_id = :v AND slot_date = :d AND half = :h AND status = 'active'
        """), {"v": vid, "d": slot_date, "h": half}).scalar_one())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=1000)
    args = ap.parse_args()
    n = args.concurrency
    base = date.today() + timedelta(days=1)

    print("=" * 74)
    print(f"档期竞争压测 · 并发 {n}")
    print("=" * 74)

    failed = 0

    # ── A. 不同幂等键抢同一档期 ──
    d1 = str(base)
    pid, vid, tid = seed(d1, 0)
    res = run_race(n, pid, vid, tid, d1, 0, same_key=False)
    booked = count_booked(vid, d1, 0)
    ok_a = (res.get("created", 0) == 1 and booked == 1)
    print(f"\n[A] {n} 个**不同**幂等键抢同一档期")
    print(f"    结果分布: {dict(res)}")
    print(f"    落库有效预约数: {booked}   （要求恰好 1）")
    print(f"    {'[OK]' if ok_a else '[FAIL]'} 重复占用 = {booked - 1}")
    failed += 0 if ok_a else 1

    # ── B. 相同幂等键并发 ──
    d2 = str(base + timedelta(days=1))
    pid2, vid2, tid2 = seed(d2, 0)
    res2 = run_race(n, pid2, vid2, tid2, d2, 0, same_key=True)
    booked2 = count_booked(vid2, d2, 0)
    created2 = res2.get("created", 0)
    replayed2 = res2.get("replayed", 0)
    ok_b = (created2 == 1 and booked2 == 1 and replayed2 == n - 1)
    print(f"\n[B] {n} 个**相同**幂等键并发")
    print(f"    结果分布: {dict(res2)}")
    print(f"    落库有效预约数: {booked2}   created={created2} replayed={replayed2}")
    print(f"    {'[OK]' if ok_b else '[FAIL]'} 幂等生效（恰好 1 次真实创建，其余全部重放）")
    failed += 0 if ok_b else 1

    # ── C. 同 key 换 body ──
    d3 = str(base + timedelta(days=2))
    pid3, vid3, tid3 = seed(d3, 0)
    key = f"idem-{uuid.uuid4().hex}"
    first = create_booking(idem_key=key, project_id=pid3, vehicle_id=vid3,
                           testers_id=tid3, slot_date=d3, half=0)
    second = create_booking(idem_key=key, project_id=pid3, vehicle_id=vid3,
                            testers_id=tid3, slot_date=d3, half=1)   # body 不同
    booked3 = count_booked(vid3, d3, 0) + count_booked(vid3, d3, 1)
    ok_c = (first.code == "created" and second.status_code == 422 and booked3 == 1)
    print("\n[C] 同幂等键换 body")
    print(f"    首次: {first.code}  第二次: {second.code}/{second.status_code}")
    print(f"    落库预约数: {booked3}   （要求 1，第二次不得产生副作用）")
    print(f"    {'[OK]' if ok_c else '[FAIL]'}")
    failed += 0 if ok_c else 1

    print("\n" + "=" * 74)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
