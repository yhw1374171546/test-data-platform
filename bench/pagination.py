"""深分页对比：`OFFSET` vs **keyset**（技术方案 §4.7）。

造 50 万条问题单，在第 1 / 1000 / 10000 / 25000 页各测一遍：

    OFFSET：  SELECT ... ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 499980
              —— 扫描 + 丢弃，越翻越慢（"深分页"的经典问题）
    KEYSET：  SELECT ... WHERE (created_at, id) < (:t, :id)
              ORDER BY created_at DESC, id DESC LIMIT 20
              —— 索引定位，深度基本不影响耗时

**但 keyset 不是"改一下 SQL 就快"**，索引必须同时覆盖「过滤条件 + 排序」。
所以这个脚本测两组口径，把这件事量出来：

  ① 全局列表（不带 project_id）：`ix_defects_keyset (created_at DESC, id DESC)` 正好命中
  ② 按项目过滤（API 真实形态）：现有索引覆盖不了 `project_id`，PG 会退化成扫描+排序
     —— 需要复合索引 `(project_id, created_at DESC, id DESC)`（脚本里会打印 DDL）

用法：
    python bench/pagination.py                    # 50 万条，两种口径都测
    python bench/pagination.py --rows 100000      # 快速验证
    python bench/pagination.py --keep             # 保留数据（默认跑完删掉）
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                                  # noqa: E402

from app.infra.db import session_scope                       # noqa: E402
from app.service.defect import list_defects                  # noqa: E402

PAGE_SIZE = 20
DEPTHS = (1, 1000, 10000, 25000)

CURRENT: Dict[str, Any] = {}


def seed(rows: int) -> Dict[str, Any]:
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = int(s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, '深分页实验项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-PAGE-{tag}"}).scalar_one())

        started = time.perf_counter()
        # created_at 往前递减 1 秒：保证 (created_at, id) 有稳定全序，能直接走索引
        s.execute(text("""
            INSERT INTO defects (defect_no, title, severity, status, project_id,
                                 created_at, updated_at)
            SELECT 'BULK-' || :tag || '-' || g,
                   'bulk defect ' || g,
                   'S3', 'open', :pid,
                   now() - (g || ' seconds')::interval,
                   now()
              FROM generate_series(1, :rows) g
        """), {"tag": tag, "pid": pid, "rows": rows})
        elapsed = time.perf_counter() - started

    return {"project_id": pid, "tag": tag, "rows": rows, "seed_ms": elapsed * 1000}


def cleanup(env: Dict[str, Any]) -> int:
    with session_scope() as s:
        deleted = s.execute(text(
            "DELETE FROM defects WHERE project_id = :pid"), {"pid": env["project_id"]})
        s.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": env["project_id"]})
    return int(deleted.rowcount or 0)


def _where(scoped: bool) -> Tuple[str, Dict[str, Any]]:
    if scoped:
        return "project_id = :pid", {"pid": CURRENT["project_id"]}
    return "1 = 1", {}


def cursor_for(offset: int, scoped: bool) -> Optional[Tuple[Any, int]]:
    """取**上一页最后一条**的游标（只取一次，不计入测量）。

    这里有个经典 off-by-one：keyset 的语义是"取游标之后（更小）的 20 条"，
    所以游标必须是**第 offset-1 条**（上一页末条），而不是第 offset 条。
    第一版取成了当页第一条，结果 keyset 返回的是下一整页，正确性校验直接红。
    第 1 页没有上一页 → 返回 None，走不带游标的查询。
    """
    if offset <= 0:
        return None
    where, params = _where(scoped)
    with session_scope() as s:
        row = s.execute(text(f"""
            SELECT created_at, id FROM defects
             WHERE {where}
             ORDER BY created_at DESC, id DESC
             LIMIT 1 OFFSET :off
        """), {**params, "off": offset - 1}).first()
    if row is None:
        return None
    return (row.created_at, int(row.id))


def run_offset(offset: int, iterations: int, scoped: bool) -> List[float]:
    where, params = _where(scoped)
    sql = text(f"""
        SELECT id, defect_no, created_at FROM defects
         WHERE {where}
         ORDER BY created_at DESC, id DESC
         LIMIT :lim OFFSET :off
    """)
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        with session_scope() as s:
            s.execute(sql, {**params, "lim": PAGE_SIZE, "off": offset}).all()
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def _keyset_sql(scoped: bool, with_cursor: bool) -> Tuple[Any, Dict[str, Any]]:
    where, params = _where(scoped)
    extra = "\n           AND (created_at, id) < (:cts, :cid)" if with_cursor else ""
    return text(f"""
        SELECT id, defect_no, created_at FROM defects
         WHERE {where}{extra}
         ORDER BY created_at DESC, id DESC
         LIMIT :lim
    """), params


def run_keyset(cursor: Optional[Tuple[Any, int]], iterations: int,
               scoped: bool) -> List[float]:
    sql, params = _keyset_sql(scoped, cursor is not None)
    timings = []
    for _ in range(iterations):
        extra = {"cts": cursor[0], "cid": cursor[1]} if cursor else {}
        started = time.perf_counter()
        with session_scope() as s:
            s.execute(sql, {**params, **extra, "lim": PAGE_SIZE}).all()
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def explain(sql: str, params: Dict[str, Any]) -> List[str]:
    with session_scope() as s:
        rows = s.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params).all()
    return [str(r[0]) for r in rows]


def percentile(timings: List[float], q: float) -> float:
    ordered = sorted(timings)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def measure(scoped: bool, iterations: int, rows: int) -> List[Dict[str, Any]]:
    label = "带 project_id 过滤（API 真实形态）" if scoped else "全局列表（索引正好命中）"
    print(f"\n{label}")
    print(f"  {'页深':>7}{'OFFSET P50':>13}{'OFFSET P99':>13}"
          f"{'KEYSET P50':>13}{'KEYSET P99':>13}{'加速':>9}")
    out: List[Dict[str, Any]] = []
    for depth in DEPTHS:
        offset = (depth - 1) * PAGE_SIZE
        if offset + PAGE_SIZE > rows:
            continue
        # 第 1 页没有游标（None），走不带游标的分支 —— 不能因为 None 就跳过
        cursor = cursor_for(offset, scoped)
        off_times = run_offset(offset, iterations, scoped)
        key_times = run_keyset(cursor, iterations, scoped)
        row = {
            "depth": depth, "offset": offset,
            "off_p50": statistics.median(off_times), "off_p99": percentile(off_times, 0.99),
            "key_p50": statistics.median(key_times), "key_p99": percentile(key_times, 0.99),
        }
        row["speedup"] = (row["off_p99"] / row["key_p99"]) if row["key_p99"] else 0.0
        out.append(row)
        print(f"  {depth:>7}{row['off_p50']:>12.2f}ms{row['off_p99']:>12.2f}ms"
              f"{row['key_p50']:>12.2f}ms{row['key_p99']:>12.2f}ms{row['speedup']:>8.1f}x")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=500_000)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="跑完保留数据（默认删除）")
    args = ap.parse_args()

    print("=" * 78)
    print(f"深分页对比 · {args.rows:,} 条问题单 · 每页 {PAGE_SIZE} 条 · "
          f"每个深度 {args.iterations} 次")
    print("=" * 78)

    env = seed(args.rows)
    CURRENT.update(env)
    print(f"造数完成：{args.rows:,} 条，耗时 {env['seed_ms']:.0f}ms")

    global_rows = measure(False, args.iterations, args.rows)

    # 带 project_id 的口径要测两次：没有复合索引 vs 有复合索引。
    # 这就是"索引必须匹配『过滤 + 排序』"的证据。
    with session_scope() as s:
        s.execute(text("DROP INDEX IF EXISTS ix_defects_project_keyset"))
    print("\n（已临时移除复合索引 ix_defects_project_keyset，测 before 组）")
    scoped_before = measure(True, args.iterations, args.rows)
    with session_scope() as s:
        s.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_defects_project_keyset
                ON defects (project_id, created_at DESC, id DESC)
        """))
    print("\n（已创建复合索引 ix_defects_project_keyset，测 after 组）")
    scoped_after = measure(True, args.iterations, args.rows)
    scoped_rows = scoped_after

    # 正确性：同一深度，两种方式必须返回同一页（全局口径）
    depth = global_rows[-1]["depth"] if global_rows else 1
    offset = (depth - 1) * PAGE_SIZE
    with session_scope() as s:
        offset_ids = [int(r.id) for r in s.execute(text("""
            SELECT id FROM defects ORDER BY created_at DESC, id DESC
             LIMIT :lim OFFSET :off
        """), {"lim": PAGE_SIZE, "off": offset}).all()]
    cursor = cursor_for(offset, False)
    sql, params = _keyset_sql(False, cursor is not None)
    with session_scope() as s:
        key_ids = [int(r.id) for r in s.execute(sql, {
            **params,
            **({"cts": cursor[0], "cid": cursor[1]} if cursor else {}),
            "lim": PAGE_SIZE}).all()]
    same_page = offset_ids == key_ids
    print(f"\n  正确性（第 {depth} 页）：两种方式返回同一页 → {same_page}")

    for scoped, label in ((False, "全局"), (True, "带 project_id")):
        rows_ = global_rows if not scoped else scoped_rows
        if not rows_:
            continue
        deep = rows_[-1]
        cursor = cursor_for(deep["offset"], scoped)
        sql, params = _keyset_sql(scoped, cursor is not None)
        print(f"\n  EXPLAIN（{label} · 第 {deep['depth']} 页 · KEYSET）")
        for line in explain(str(sql), {
                **params,
                **({"cts": cursor[0], "cid": cursor[1]} if cursor else {}),
                "lim": PAGE_SIZE}):
            print(f"    {line}")

    # 服务层（真实 API 路径）
    api_cursor = cursor_for((global_rows[-1]["offset"] if global_rows else 0), False)
    started = time.perf_counter()
    page = list_defects(limit=PAGE_SIZE,
                        cursor=f"{api_cursor[0].isoformat()}|{api_cursor[1]}")
    api_ms = (time.perf_counter() - started) * 1000
    print(f"\n  服务层 list_defects（keyset 深页）：{api_ms:.2f}ms，"
          f"返回 {len(page['items'])} 条，has_more={page['has_more']}")

    print("\n" + "=" * 78)
    print("校验")
    print("=" * 78)
    failed = 0
    checks: List[Tuple[str, bool, str]] = []

    if len(global_rows) >= 2:
        first, last = global_rows[0], global_rows[-1]
        checks.append((
            "OFFSET 随页深变慢（扫描+丢弃）",
            last["off_p99"] >= first["off_p99"],
            f"第 {first['depth']} 页 {first['off_p99']:.1f}ms → "
            f"第 {last['depth']} 页 {last['off_p99']:.1f}ms"))
        checks.append((
            "KEYSET 随页深基本不变（索引定位）",
            last["key_p99"] <= max(first["key_p99"] * 3, first["key_p99"] + 5),
            f"第 {first['depth']} 页 {first['key_p99']:.1f}ms → "
            f"第 {last['depth']} 页 {last['key_p99']:.1f}ms"))
        checks.append((
            "全局口径：深页 KEYSET 明显快于 OFFSET",
            last["key_p99"] < last["off_p99"],
            f"OFFSET {last['off_p99']:.1f}ms vs KEYSET {last['key_p99']:.1f}ms"
            f"（{last['speedup']:.1f}x）"))
    checks.append(("两种方式返回同一页（正确性）", same_page, f"第 {depth} 页"))

    if scoped_before and scoped_after:
        # 浅页最能体现"索引不匹配"：没有复合索引时，越靠前的页要过滤/排序的行越多
        head_before, head_after = scoped_before[0], scoped_after[0]
        checks.append((
            "加复合索引后「带 project_id」的浅页 keyset 明显变快",
            head_after["key_p99"] < head_before["key_p99"],
            f"第 {head_before['depth']} 页 {head_before['key_p99']:.1f}ms → "
            f"{head_after['key_p99']:.1f}ms"))

    for label, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}：{detail}")
        failed += 0 if ok else 1

    if scoped_before and scoped_after:
        print("\n  ★ 一个必须写进报告的发现：keyset 不是「改 SQL 就快」")
        print("    同一句 keyset SQL，只是索引不同：")
        print(f"      {'页深':>7}{'无复合索引 P99':>16}{'有复合索引 P99':>16}")
        for b, a in zip(scoped_before, scoped_after, strict=False):
            print(f"      {b['depth']:>7}{b['key_p99']:>14.1f}ms{a['key_p99']:>14.1f}ms")
        print("    原因：基线只建了 (created_at DESC, id DESC)，覆盖不了过滤条件 project_id，")
        print("    PG 只能「扫项目下的行 → 过滤 → 排序」。补一条复合索引就好了：")
        print("      CREATE INDEX CONCURRENTLY ix_defects_project_keyset")
        print("          ON defects (project_id, created_at DESC, id DESC);")
        print("    这条索引已经落在 migrations/versions/002_perf_indexes.sql 里（幂等，")
        print("    init 会补建）。")

    if not args.keep:
        removed = cleanup(env)
        print(f"\n  清理：删除 {removed:,} 条实验数据")

    print("\n" + "=" * 78)
    print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
