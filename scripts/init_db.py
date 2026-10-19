"""建库 + 迁移到最新版本 + 演示数据：**幂等，可以重复执行**。

为什么不用 psql.exe：
`dev.ps1` 要能在"只装了 Python"的机器上跑起来。psycopg 本来就是服务依赖，
直接在同一个进程里建库/跑迁移，比要求用户去装 pgAdmin/psql 现实得多。

三个行为刻意设计成这样：
· 库已存在 → 跳过，不报错（一键脚本必须能重复跑）；
· 表结构 → 交给 Alembic `upgrade head`；**老库（有表但没有 alembic_version）先 stamp 到 head**，
  因为它的形状已经是最新的，不需要再跑一遍 DDL；
· `--seed-demo` → 演示主数据，用 ON CONFLICT 保证重复执行不会插重。

`--drop` 是唯一的破坏性操作，专门给"我想从头来一遍"用。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Any, Dict, List

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import psycopg                                              # noqa: E402
from alembic import command as alembic_command             # noqa: E402
from alembic.config import Config as AlembicConfig          # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from app.infra.auth import hash_password                     # noqa: E402
from app.infra.db import DATABASE_URL                        # noqa: E402

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

# 演示账号：**只用于本地/演示环境**，生产必须改口令。
# 三个角色刚好覆盖权限矩阵：admin 能重放死信、tester 能写业务、viewer 只能读。
DEMO_USERS = [
    ("admin", "Admin@12345", "admin"),
    ("tester", "Tester@12345", "tester"),
    ("viewer", "Viewer@12345", "viewer"),
]


def demo_users() -> list[tuple[str, str, str]]:
    """演示账号清单；线上用环境变量补一个"只读演示账号"。

    `TDP_DEMO_USER` / `TDP_DEMO_PASSWORD` 只创建一个 **viewer** 账号：
    面试官/HR 能点开三个页面看数据，但改不了任何东西（写操作会被 403）。
    这比给 admin 安全得多 —— 公网上谁都能点的 admin 等于没有权限。
    """
    users = list(DEMO_USERS)

    # 线上可以把 admin 的初始口令从环境变量注入，这样即使忘了改口令，
    # 公网上的 admin 也不是众所周知的那一个（比"记得改"更可靠）。
    admin_password = (os.environ.get("TDP_ADMIN_PASSWORD") or "").strip()
    if admin_password:
        users = [(u, admin_password, r) if u == "admin" else (u, pw, r) for u, pw, r in users]

    username = (os.environ.get("TDP_DEMO_USER") or "").strip()
    password = (os.environ.get("TDP_DEMO_PASSWORD") or "").strip()
    if username and password:
        users = [u for u in users if u[0] != username]
        users.append((username, password, "viewer"))
    return users


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


def schema_version(dsn: str) -> str:
    """当前 alembic 版本号；没有版本表（老库）返回空串。"""
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL").fetchone()
        if not row or not row[0]:
            return ""
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else ""


def alembic_config(dsn: str) -> AlembicConfig:
    """在代码里构造配置，不依赖当前工作目录，也不依赖 alembic.ini 是否被拷进镜像。"""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(BASE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace(
        "postgresql://", "postgresql+psycopg://", 1))
    return cfg


def upgrade_schema(dsn: str) -> str:
    """把库迁到 head。返回 'upgraded' / 'stamped'（老库只补版本号）/ 'already'。"""
    tables = table_count(dsn)
    version = schema_version(dsn)

    if tables == 0:
        alembic_command.upgrade(alembic_config(dsn), "head")
        return "upgraded"

    if version:
        alembic_command.upgrade(alembic_config(dsn), "head")
        return "already"

    # 老库：表已经是最新形状（手工跑过 001+002），只缺版本号。
    # 直接 upgrade 会因为"表已存在"而失败，所以先 stamp 到 head。
    alembic_command.stamp(alembic_config(dsn), "head")
    return "stamped"


def drop_database(dsn: str) -> bool:
    """删库（破坏性）。返回 True 表示确实删掉了。"""
    info = conninfo_to_dict(dsn)
    name = info.get("dbname") or "tdp"
    admin = make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            return False
        conn.execute(f'DROP DATABASE "{name.replace(chr(34), chr(34) * 2)}"')
        return True


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

        # 演示账号：口令用 PBKDF2 哈希入库（绝不存明文）。
        # 重复执行会重置口令——这正是"忘了密码就跑一次 init"的用法。
        for username, password, role in demo_users():
            conn.execute("""
                INSERT INTO app_users (username, password_hash, role, active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (username) DO UPDATE
                   SET password_hash = EXCLUDED.password_hash,
                       role = EXCLUDED.role,
                       active = TRUE
            """, (username, hash_password(password), role))

        # ABAC：把非管理员演示账号加进演示项目。
        # admin 不需要成员关系（管理员直通），这本身也是一条可讲的权限设计。
        for username, _password, role in demo_users():
            if role == "admin":
                continue
            conn.execute("""
                INSERT INTO project_members (project_id, username) VALUES (%s, %s)
                ON CONFLICT (project_id, username) DO NOTHING
            """, (pid, username))

        stats = {
            "projects": _scalar(conn, "SELECT count(*) FROM projects"),
            "vehicles": _scalar(conn, "SELECT count(*) FROM vehicles"),
            "versions": _scalar(conn, "SELECT count(*) FROM versions"),
            "testers": _scalar(conn, "SELECT count(*) FROM testers"),
            "test_cases": _scalar(conn, "SELECT count(*) FROM test_cases"),
            "execution_queue": _scalar(conn, "SELECT count(*) FROM execution_queue"),
            "app_users": _scalar(conn, "SELECT count(*) FROM app_users"),
            "project_members": _scalar(conn, "SELECT count(*) FROM project_members"),
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
    ap.add_argument("--drop", action="store_true",
                    help="先删库再重建（破坏性操作，专门给「我想从头来一遍」用）")
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
              f"db={conninfo_to_dict(dsn).get('dbname', '?')} "
              f"schema={schema_version(dsn) or '-'}")
        return 0 if tables >= 16 else 3

    try:
        if args.drop:
            dropped = drop_database(dsn)
            say(f"[init] 已删库（{'删掉了' if dropped else '本来就不存在'}）")

        created = ensure_database(dsn)
        say(f"[init] 数据库 {'已创建' if created else '已存在'}"
            f"（dbname={conninfo_to_dict(dsn).get('dbname', '?')}）")

        # 表结构统一交给 Alembic：新库 upgrade 到 head，老库只补版本号
        mode = upgrade_schema(dsn)
        note = {"upgraded": "已执行全部迁移",
                "stamped": "老库没有版本表，已 stamp 到 head（表结构本来就是最新的）",
                "already": "已是最新版本"}[mode]
        say(f"[init] schema {note}：{schema_version(dsn)} "
            f"（{table_count(dsn)} 张表 / {index_count(dsn)} 个索引）")

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
