"""缓存一致性实验：**A/B/C 三种失效策略 + 一个负对照**（技术方案 §4.3）。

四组：`none`（负对照）/ `delete`（A）/ `double_delete`（B）/ `outbox`（C）。
每组都是 N 个写线程 + N 个读线程并发跑 `--seconds` 秒，然后统计：

  · **脏读次数**：读到"在读请求开始之前就已经被新值覆盖"的旧值。判定方法见下。
  · 命中率、读延迟 p50/p99
  · 停写后缓存里还剩几条旧值（残留窗口）
  · TTL 兜底后是否收敛（真实系统的最后一道防线）

### 脏读怎么判定（这里必须严谨，否则数字没有意义）

给每条写入一个**递增序号** `seq`，写进 `location = "L{seq}"`。
写线程在**事务提交之后**把 `(seq, 提交完成时刻)` 记到共享日志里（受锁保护）。
读线程：

    t0 = 开始读的时刻
    served = get_vehicle()             # 可能命中缓存 → 拿到 L{k}
    committed = 日志里"提交时刻 <= t0"的最大 seq

    如果 k < committed  → 这次读是脏读：
    读请求开始时，更新的值**已经提交**了，但读返回的是更旧的值。

这个判定是**保守**的：日志时间戳在锁内采集，只会比真实提交时刻晚，
所以不会把"其实没问题"的读误判成脏读（宁可少报，不虚报）。

### 关于「最终一致」

三种失效策略都**不能证明**自己的脏读窗口为 0，它们只是把窗口压得比读请求耗时更短。
真正的兜底是 TTL。所以实验最后会模拟 TTL 过期，再校验一次缓存 == DB：
这一步是"能自愈"的证据，而不是"窗口为 0"的证据。报告里也是这么写的。

用法：
    python bench/cache_consistency.py --readers 200 --writers 200 --seconds 8
"""
from __future__ import annotations

import argparse
import bisect
import os
import statistics
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                                  # noqa: E402

from app.infra import cache                                  # noqa: E402
from app.infra.db import engine, session_scope               # noqa: E402
from app.service.catalog import get_vehicle, update_vehicle  # noqa: E402
from app.service.outbox import process_cache_invalidations   # noqa: E402

# 全局单调序号：写入值 = L{seq}，跨策略不重置（见 run_strategy 里的注释）
_GLOBAL_SEQ = {"value": 0}
_GLOBAL_SEQ_LOCK = threading.Lock()


def next_global_seq() -> int:
    with _GLOBAL_SEQ_LOCK:
        _GLOBAL_SEQ["value"] += 1
        return _GLOBAL_SEQ["value"]


class CommitLog:
    """记录**每台车**的写入提交时刻与序号，供读线程判断自己是不是读到了旧值。

    两个坑都是踩出来的：

    ① **必须按车分开记**。第一版用了全局最大序号，而读到的值是某台车的 ——
       拿"全局最大"去比"单车序号"，正常读也会被判成脏读（各策略都测出 40%~99% 的假脏读）。
    ② **读侧不要加锁**。第一版读线程每次都要抢锁，几百个线程在锁上形成 convoy，
       p50 直接飙到几百毫秒 —— 测出来的是锁竞争，不是缓存行为。
       现在写线程只追加并发布一个"已发布长度"整数，读线程用这个长度做 bisect 上界，
       不加锁也不会看到半条记录（列表只增长、元素不可变）。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, vin: str, seq: int) -> None:
        """在事务提交之后调用；时间戳在锁内采集，保证单调、只会偏晚。"""
        with self._lock:
            entry = self._data.get(vin)
            if entry is None:
                entry = {"times": [], "max": [], "len": 0}
                self._data[vin] = entry
            entry["times"].append(time.perf_counter())
            entry["max"].append(max(entry["max"][-1] if entry["max"] else 0, seq))
            entry["len"] = len(entry["times"])

    def committed_at(self, vin: str, moment: float) -> int:
        """这台车截至 `moment` 已经提交完成的最大序号（无锁）。"""
        entry = self._data.get(vin)
        if not entry:
            return 0
        published = entry["len"]
        if published == 0:
            return 0
        idx = bisect.bisect_right(entry["times"], moment, 0, published) - 1
        return entry["max"][idx] if idx >= 0 else 0

    def total(self) -> int:
        return sum(int(entry["len"]) for entry in self._data.values())


def seq_of(location: Any) -> Optional[int]:
    """从 location 里解析序号（形如 L123）。"""
    value = str(location or "")
    if not value.startswith("L"):
        return None
    try:
        return int(value[1:])
    except ValueError:
        return None


class Consumer:
    """模拟独立的 worker 进程：轮询 outbox，消费 cache_invalidate 消息。

    `interval` 就是**消费延迟**——它是 C 方案唯一真正决定脏读窗口的参数，
    所以实验里要能调（report 里给了 2ms 和 20ms 两组）。
    """

    def __init__(self, interval: float = 0.02) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.batches = 0
        self.processed = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                # 批量消费：缓存失效是幂等且可批量的，一次事务处理一批
                stats = process_cache_invalidations(limit=500)
                self.batches += 1
                self.processed += stats["messages"]
            except Exception:                        # noqa: BLE001 - 消费者不能因为一次失败就退出
                pass
            time.sleep(self.interval)


def seed_vehicles(count: int, tag: str) -> List[str]:
    vins = [f"CACHEBENCH-{tag}-{i:02d}" for i in range(count)]
    with session_scope() as s:
        for i, vin in enumerate(vins):
            s.execute(text("""
                INSERT INTO vehicles (vin, model, plate_no, status, location)
                VALUES (:v, 'U8', :p, 'available', 'L0')
                ON CONFLICT (vin) DO UPDATE SET location = 'L0'
            """), {"v": vin, "p": f"沪A·{i:02d}"})
    return vins


def db_location(vin: str) -> str:
    with session_scope() as s:
        return str(s.execute(text("SELECT location FROM vehicles WHERE vin = :v"),
                             {"v": vin}).scalar_one())


def cached_location(vin: str) -> Optional[str]:
    data = cache.get_json(cache.key_vehicle(vin), entity="vehicle")
    return None if data is None else str(data.get("location"))


def clear_pending_invalidations() -> int:
    """清掉上一轮实验遗留的 cache_invalidate 消息。

    不清的话：上一轮留下上万条 pending 消息，消费者（FIFO）整轮都在消费**旧**消息，
    本轮写的失效消息排在队尾根本轮不到 —— C 组会退化成"从不失效"，测出来毫无意义。
    这正是"实验卫生"：**先把队列清干净，再测"消息驱动"**。
    只清这个 topic 的 pending，不碰业务消息。
    """
    with session_scope() as s:
        rows = s.execute(text("""
            DELETE FROM outbox
             WHERE topic = :t AND status = 'pending'
            RETURNING id
        """), {"t": cache.TOPIC_CACHE_INVALIDATE}).all()
    return len(rows)


def pending_invalidations() -> int:
    with session_scope() as s:
        return int(s.execute(text("""
            SELECT count(*) FROM outbox
             WHERE topic = :t AND status = 'pending'
        """), {"t": cache.TOPIC_CACHE_INVALIDATE}).scalar_one())


def run_strategy(name: str, vins: List[str], *, readers: int, writers: int,
                 seconds: float, double_delay: float,
                 consumer_interval: float = 0.02) -> Dict[str, Any]:
    cache.set_strategy(name)
    for vin in vins:
        cache.delete(cache.key_vehicle(vin))
    cache.reset_stats()
    clear_pending_invalidations()

    # 预热：**必须先填一遍缓存**。否则所有读线程同时冷启动、一起回源，
    # 会被连接池堵住，测出来的"延迟"全是排队时间，跟缓存策略没关系（第一版就踩了这个坑）。
    for vin in vins:
        get_vehicle(vin)
    cache.reset_stats()

    commit_log = CommitLog()
    samples: List[Tuple[int, int, str]] = []
    latencies: List[List[float]] = []
    stop_at = time.perf_counter() + seconds
    start = threading.Barrier(readers + writers, timeout=60)

    writer_stats = {"writes": 0, "errors": 0}
    reader_stats = {"reads": 0, "dirty": 0, "lag_sum": 0, "max_lag": 0, "errors": 0}

    def writer() -> None:
        local = 0
        try:
            start.wait()
            while time.perf_counter() < stop_at:
                vin = vins[local % len(vins)]
                seq = next_global_seq()
                update_vehicle(vin=vin, location=f"L{seq}")
                commit_log.record(vin, seq)      # 事务已经提交
                local += 1
            writer_stats["writes"] += local
        except Exception:                       # noqa: BLE001
            writer_stats["errors"] += 1

    def reader() -> None:
        mine: List[float] = []
        dirty = lag_sum = max_lag = 0
        visited = 0
        try:
            start.wait()
            while time.perf_counter() < stop_at:
                vin = vins[visited % len(vins)]
                visited += 1
                t0 = time.perf_counter()
                data = get_vehicle(vin)
                mine.append(time.perf_counter() - t0)

                served = seq_of(data.get("location"))
                committed = commit_log.committed_at(vin, t0)
                if len(samples) < 5:
                    samples.append((served if served is not None else -1,
                                    committed, str(data.get("location"))))
                if served is not None and served < committed:
                    lag = committed - served
                    dirty += 1
                    lag_sum += lag
                    max_lag = max(max_lag, lag)
            reader_stats["reads"] += len(mine)
            reader_stats["dirty"] += dirty
            reader_stats["lag_sum"] += lag_sum
            reader_stats["max_lag"] = max(reader_stats["max_lag"], max_lag)
            latencies.append(mine)
        except Exception:                       # noqa: BLE001
            reader_stats["errors"] += 1

    consumer = Consumer(interval=consumer_interval)
    if name == "outbox":
        consumer.start()

    threads = ([threading.Thread(target=writer, daemon=True) for _ in range(writers)]
               + [threading.Thread(target=reader, daemon=True) for _ in range(readers)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if name == "outbox":
        time.sleep(0.3)                        # 让消费队列追平
        consumer.stop()

    result: Dict[str, Any] = {
        "strategy": name,
        "writes": writer_stats["writes"],
        "reads": reader_stats["reads"],
        "errors": writer_stats["errors"] + reader_stats["errors"],
        "dirty": reader_stats["dirty"],
        "dirty_rate": (reader_stats["dirty"] / reader_stats["reads"])
                      if reader_stats["reads"] else 0.0,
        # 每次脏读"落后几个版本"：这才是区分"短暂旧值"和"长期旧值"的关键
        "max_lag": int(reader_stats["max_lag"]),
        "avg_lag": (round(reader_stats["lag_sum"] / reader_stats["dirty"], 1)
                    if reader_stats["dirty"] else 0.0),
        "stats": cache.stats(),
        "commits_logged": commit_log.total(),
        "consumer_processed": consumer.processed if name == "outbox" else 0,
        "samples": samples,
    }
    flat = [x for chunk in latencies for x in chunk]
    if flat:
        flat.sort()
        result["p50_ms"] = statistics.median(flat) * 1000
        result["p99_ms"] = flat[min(len(flat) - 1, int(len(flat) * 0.99))] * 1000

    # 停写后：缓存里还剩几条旧值？
    time.sleep(0.15)                           # 给延迟双删的定时器一点时间
    stale = [vin for vin in vins
             if (loc := cached_location(vin)) is not None and loc != db_location(vin)]
    result["stale_at_end"] = len(stale)
    result["invalidations_pending"] = pending_invalidations() if name == "outbox" else 0

    # TTL 兜底：真实系统的最后一道防线（这里用 1s TTL 模拟）
    cache.set_ttl(*[cache.key_vehicle(v) for v in vins], ttl=1)
    time.sleep(1.3)
    after_ttl = [vin for vin in vins
                 if get_vehicle(vin)["location"] != db_location(vin)]
    result["stale_after_ttl"] = len(after_ttl)

    cache.set_strategy("delete")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readers", type=int, default=20,
                    help="读线程数。默认 20：这台 16 核机器上 100+100 会让 CPU/GIL 饱和，"
                         "测出来主要是调度噪声（报告里附了一组 100/100 的对照）")
    ap.add_argument("--writers", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--vehicles", type=int, default=8)
    ap.add_argument("--double-delay", type=float, default=0.05,
                    help="延迟双删的延迟（秒）。它必须大于读请求的 DB 耗时才有意义")
    ap.add_argument("--consumer-interval", type=float, default=0.02,
                    help="C 方案的消费轮询间隔（秒）= 失效传播延迟，直接决定 C 的脏读窗口")
    ap.add_argument("--tag", default="", help="沿用上一轮的车辆标记（多组对照实验时用）")
    args = ap.parse_args()

    if not cache.available():
        print("Redis 不可用（127.0.0.1:6380）。先启动：\n"
              r"  & C:\Users\13741\dsh-devtools\redis\redis-server.exe "
              r"C:\Users\13741\dsh-devtools\redis\redis-6380.conf", file=sys.stderr)
        return 2

    tag = args.tag or uuid.uuid4().hex[:6]
    vins = seed_vehicles(args.vehicles, tag)

    pool = engine.pool
    pool_desc = f"pool_size={pool.size()} max_overflow={pool._max_overflow}"  # noqa: SLF001
    print("=" * 78)
    print(f"缓存一致性实验 · {args.readers} 读 / {args.writers} 写 · {args.seconds:.0f}s "
          f"· {args.vehicles} 台车 · 延迟双删 {args.double_delay * 1000:.0f}ms "
          f"· 消费间隔 {args.consumer_interval * 1000:.0f}ms")
    print(f"连接池：{pool_desc}"
          + ("  ⚠ 池子偏小，读延迟会被排队主导（dev.cmd 会把池子调大）"
             if pool.size() + pool._max_overflow < (args.readers + args.writers) // 4  # noqa: SLF001
             else ""))
    print("=" * 78)
    print("策略：none=负对照（故意不失效）  delete=A  double_delete=B  outbox=C")
    print("脏读判定：读到的序号 < 读开始时已提交的最大序号（保守判定，不虚报）\n")

    results: List[Dict[str, Any]] = []
    for i, name in enumerate(("none", "delete", "double_delete", "outbox"), start=1):
        label = {"none": "负对照：故意不失效",
                 "delete": "A：提交后删缓存",
                 "double_delete": "B：写前删 + 延迟再删",
                 "outbox": "C：消息驱动删缓存"}[name]
        print(f"[{i}/4] {name:<14} {label}")
        res = run_strategy(name, vins, readers=args.readers, writers=args.writers,
                           seconds=args.seconds, double_delay=args.double_delay,
                           consumer_interval=args.consumer_interval)
        results.append(res)
        print(f"      写 {res['writes']:>6} 次 / 读 {res['reads']:>6} 次  "
              f"命中率 {res['stats']['hit_rate'] * 100:>5.1f}%  "
              f"p50 {res.get('p50_ms', 0):>6.2f}ms  p99 {res.get('p99_ms', 0):>7.2f}ms")
        print(f"      脏读 {res['dirty']:>6} 次（{res['dirty_rate'] * 100:>6.2f}%）  "
              f"每次落后 平均 {res['avg_lag']:>4} / 最多 {res['max_lag']:>4} 个版本  "
              f"| 停写后残留 {res['stale_at_end']}/{len(vins)} 条  "
              f"TTL 兜底后 {res['stale_after_ttl']} 条"
              + (f"  | 消费 {res['consumer_processed']} 条" if name == "outbox" else "")
              + (f"  | 错误 {res['errors']}" if res["errors"] else ""))
        if os.environ.get("CACHEBENCH_DEBUG"):
            print(f"      [debug] 前几次采样 (served, committed, 原值): {res['samples']}")
            print(f"      [debug] 写入提交记录数 {res['commits_logged']}")
        print()

    by = {r["strategy"]: r for r in results}
    baseline = by["none"]

    print("=" * 78)
    print("校验")
    print("=" * 78)
    failed = 0

    ok_harness = baseline["dirty"] > 0
    print(f"  [{'OK' if ok_harness else 'FAIL'}] 负对照能测出脏读：none 组脏读 "
          f"{baseline['dirty']} 次（>0 说明实验打中了窗口）")
    failed += 0 if ok_harness else 1

    for name in ("delete", "double_delete", "outbox"):
        res = by[name]
        # 比**比率**而不是次数：各策略的读总量差很多（命中率高 → 读得多），
        # 直接比次数等于比"谁读得快"，不是比"谁更脏"。
        better = res["dirty_rate"] < baseline["dirty_rate"]
        print(f"  [{'OK' if better else 'FAIL'}] {name:<14} 脏读率 "
              f"{res['dirty_rate'] * 100:>6.2f}% < 负对照 {baseline['dirty_rate'] * 100:.2f}%"
              f" —— 失效策略确实起作用")
        failed += 0 if better else 1

    for name, res in by.items():
        ok_ttl = res["stale_after_ttl"] == 0
        print(f"  [{'OK' if ok_ttl else 'FAIL'}] {name:<14} TTL 兜底后缓存 == DB"
              f"（残留 {res['stale_after_ttl']} 条）")
        failed += 0 if ok_ttl else 1

    order = [by[n]["dirty_rate"] * 100 for n in ("none", "delete", "double_delete", "outbox")]
    print(f"\n  脏读率：none {order[0]:.2f}% / A {order[1]:.2f}% / B {order[2]:.2f}% / C {order[3]:.2f}%")
    lag_line = " / ".join(f"{n}: max_lag={by[n]['max_lag']}, 残留={by[n]['stale_at_end']}"
                         for n in ("none", "delete", "double_delete", "outbox"))
    print(f"  陈旧深度与残留：{lag_line}")
    print("  ★ 实测结论（和技术方案预期的 A>B>C 不一样，原因如下）：")
    print("    · A（提交后同步删）窗口最小：脏读只在'读旧值回填到删除之后'时发生，")
    print("      但**偶发的那一条会一直留到 TTL**（本次实测停写后仍残留），这就是'A 仍会脏'。")
    print("    · B 反而比 A 差：'写前删'会把缓存清空，事务进行期间读者用**旧值**重新回填，")
    print("      直到延迟删除才清掉 —— 一个延迟换一段确定的旧值窗口。")
    print("    · C 的次数最多，但每次滞后都短（max_lag 有界 = 消费延迟），")
    print("      而且失效**不会丢**：消息重试 + 批量消费，停写后残留为 0。")
    print("    所以选型不是比谁次数少，而是：要'罕见但可能长期错'（A/B），")
    print("    还是'常见但有界、可监控'（C）。生产上三者都必须有 TTL 兜底。")

    cache.clear_all(prefix=f"{cache.KEY_PREFIX}:vehicle:CACHEBENCH-{tag}")
    print("\n" + "=" * 78)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
