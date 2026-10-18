"""建库 + 建表 + 演示数据：**幂等，可以重复执行**。

为什么不用 psql.exe：
`dev.ps1` 要能在"只装了 Python"的机器上跑起来。psycopg 本来就是服务依赖，
直接在同一个进程里建库/建表，比要求用户去装 pgAdmin/psql 现实得多。

三个行为刻意设计成这样：
· 库已存在 → 跳过，不报错（一键脚本必须能重复跑）；
· 表已存在 → 跳过建表（不覆盖数据）；想重建请自己 drop database；
· `--seed-demo` → 演示主数据，用 ON CONFLICT 保证重复执行不会插重。

建表在一个事务里执行：SQL 中途报错会整体回滚，不会留下"建了一半的表"。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any, Dict, List

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import psycopg                                              # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from app.infra.db import DATABASE_URL                        # noqa: E402

BASELINE = BASE / "migrations" / "versions" / "001_baseline.sql"
# 002 之后的增补脚本：必须幂等（每次 init 都重跑）
EXTRA_MIGRATIONS = sorted(
    p for p in (BASE / "migrations" / "versions").glob("00[2-9]*.sql"))

# ── 演示数据（全部来自"整车测试"这个真实场景，不是 foo/bar）──
DEMO_PROJECT = ("U8-CITY-NOA-2025Q3", "U8 城市 NOA 2025Q3", "U8")
DEMO_VEHICLES = [
    ("LDP29A9U8RA000001", "U8", "沪A·T001", "临港试验场"),
    ("LDP29A9U8RA000002", "U8", "沪A·T002", "临港试验场"),
    ("LDP29A9U8RA000003", "U8", "沪A·T003", "安亭试验场"),
]
DEMO_VERSIONS = [("U8", "V1.2.3"), ("U8", "V1.2.4-rc1")]
DEMO_TESTERS = [("E1001", "测试员甲", "测试一组"),
                ("E1002", "测试员乙", "测试一组"),
                ("E1003", "测试员丙", "测试二组")]
DEMO_CASES = [
    ("TC-NOA-0101", "领航", "城市 NOA 无保护左转", "P0"),
    ("TC-NOA-0102", "领航", "城市 NOA 加塞避让", "P0"),
    ("TC-NOA-0103", "领航", "城市 NOA 施工区绕行", "P1"),
    ("TC-PARK-0201", "泊车", "记忆泊车跨层寻位", "P1"),
    ("TC-HMI-0301", "HMI", "NOA 激活失败提示语", "P2"),
]


def to_libpq(dsn: str) -> str:
    """SQLAlchemy 的 `postgresql+psycopg://` → psycopg 能认的 `postgresql://`。"""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


def ensure_database(dsn: str) -> bool:
    """库不存在就建。返回 True 表示本次新建。"""
    info = conninfo_to_dict(dsn)
    name = info.get("dbname") or "tdp"
    admin = make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        # CREATE DATABASE 不支持参数占位符，只能拼标识符；双引号内的双引号需要转义
        conn.execute(f'CREATE DATABASE "{name.replace(chr(34), chr(34) * 2)}"')
        return True


def table_count(dsn: str) -> int:
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        return int(conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'").fetchone()[0])


def index_count(dsn: str) -> int:
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        return int(conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'").fetchone()[0])


def apply_baseline(dsn: str) -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute(sql)                     # 无参数 → 走简单查询协议，可执行多语句
        conn.commit()                         # 中途报错则整体回滚


def apply_extra_migrations(dsn: str) -> None:
    """执行 `migrations/versions/00[2-9]*.sql` 里的增补 DDL。

    约定：这些文件里的每条语句都必须是幂等的（`CREATE INDEX IF NOT EXISTS` 之类），
    因为每次 `init` 都会重跑。真正的 Alembic 化是后面的工程项，这里先用最小代价
    让"索引能随代码演进"。
    """
    for script in sorted(EXTRA_MIGRATIONS):
        sql = script.read_text(encoding="utf-8")
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute(sql)
            conn.commit()


def seed_demo(dsn: str) -> Dict[str, int]:
    """演示主数据 + 一批 pending 执行任务（week 2 的抢占领取直接能用）。"""
    stats: Dict[str, int] = {}
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        pid = conn.execute("""
            INSERT INTO projects (code, name, model) VALUES (%s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, DEMO_PROJECT).fetchone()[0]

        for vin, model, plate, loc in DEMO_VEHICLES:
            conn.execute("""
                INSERT INTO vehicles (vin, model, plate_no, location) VALUES (%s, %s, %s, %s)
                ON CONFLICT (vin) DO NOTHING
            """, (vin, model, plate, loc))

        for model, ver in DEMO_VERSIONS:
            conn.execute("""
                INSERT INTO versions (model, ver) VALUES (%s, %s)
                ON CONFLICT (model, ver) DO NOTHING
            """, (model, ver))

        for emp, name, team in DEMO_TESTERS:
            conn.execute("""
                INSERT INTO testers (employee_no, name, team) VALUES (%s, %s, %s)
                ON CONFLICT (employee_no) DO NOTHING
            """, (emp, name, team))

        for case_no, module, title, prio in DEMO_CASES:
            conn.execute("""
                INSERT INTO test_cases (case_no, module, title, priority)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (case_no) DO NOTHING
            """, (case_no, module, title, prio))

        versions = conn.execute("SELECT id FROM versions WHERE model = 'U8' ORDER BY id").fetchall()
        cases = conn.execute("SELECT id FROM test_cases ORDER BY id").fetchall()
        for (vid,) in versions:
            for (cid,) in cases:
                conn.execute("""
                    INSERT INTO execution_queue (project_id, version_id, case_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (project_id, version_id, case_id) DO NOTHING
                """, (pid, vid, cid))

        stats = {
            "projects": _scalar(conn, "SELECT count(*) FROM projects"),
            "vehicles": _scalar(conn, "SELECT count(*) FROM vehicles"),
            "versions": _scalar(conn, "SELECT count(*) FROM versions"),
            "testers": _scalar(conn, "SELECT count(*) FROM testers"),
            "test_cases": _scalar(conn, "SELECT count(*) FROM test_cases"),
            "execution_queue": _scalar(conn, "SELECT count(*) FROM execution_queue"),
        }
        conn.commit()
    return stats


def _scalar(conn: Any, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="初始化测试数据管理平台的 PostgreSQL 库")
    ap.add_argument("--dsn", default=to_libpq(DATABASE_URL),
                    help="libpq DSN，默认取 TDP_DATABASE_URL")
    ap.add_argument("--seed-demo", action="store_true", help="写入演示主数据（可重复执行）")
    ap.add_argument("--check", action="store_true",
                    help="只体检：打印 tables=/indexes=，退出码 0=就绪 2=连不上 3=未建表")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    dsn = to_libpq(args.dsn)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if args.check:
        # 给 dev.ps1 用的机器可读体检：一行输出 + 语义化退出码，
        # 比在 PowerShell 里内联 Python 片段稳得多。
        try:
            tables, indexes = table_count(dsn), index_count(dsn)
        except psycopg.OperationalError as exc:
            print(f"[check] 连不上 PostgreSQL：{exc}", file=sys.stderr)
            return 2
        print(f"tables={tables} indexes={indexes} "
              f"db={conninfo_to_dict(dsn).get('dbname', '?')}")
        return 0 if tables >= 16 else 3

    try:
        created = ensure_database(dsn)
        say(f"[init] 数据库 {'已创建' if created else '已存在'}"
            f"（dbname={conninfo_to_dict(dsn).get('dbname', '?')}）")

        tables = table_count(dsn)
        if tables == 0:
            if not BASELINE.exists():
                print(f"[init] 找不到基线 DDL：{BASELINE}", file=sys.stderr)
                return 1
            apply_baseline(dsn)
            tables = table_count(dsn)
            say(f"[init] 已建表 {tables} 张 / 索引 {index_count(dsn)} 个")
        else:
            say(f"[init] 表已存在，跳过建表（{tables} 张 / 索引 {index_count(dsn)} 个）")

        # 增补索引：全部 IF NOT EXISTS，每次 init 都跑一遍是安全的。
        # 001 是一次性建表，之后的索引改动放 002 —— 没有 Alembic 之前，
        # 这是"能增量演进又不破坏已有库"的最小做法。
        before = index_count(dsn)
        apply_extra_migrations(dsn)
        after = index_count(dsn)
        if after > before:
            say(f"[init] 已补建索引 {after - before} 个（合计 {after} 个）")

        if args.seed_demo:
            stats = seed_demo(dsn)
            say("[init] 演示数据：" + "  ".join(f"{k}={v}" for k, v in stats.items()))

    except psycopg.OperationalError as exc:
        print("[init] 连不上 PostgreSQL。请先启动数据库（或用 dev.ps1 init 自动拉起）：",
              file=sys.stderr)
        print(f"       pg_ctl -D <pgdata 目录> -o '-p {conninfo_to_dict(dsn).get('port', 5433)}' "
              "-l pg.log start", file=sys.stderr)
        print(f"       原始错误：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
