"""baseline：16 张表 + 37 个索引

DDL 的单一来源是 `migrations/sql/001_baseline.sql`（人可以直接 `psql -f` 跑，
也可以在 Alembic 里按版本推进）。这个迁移只负责"在版本链上占住第 1 位"。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-18
"""
from __future__ import annotations

import pathlib

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"


def _run_script(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    # 用 DBAPI 连接执行：psycopg3 在没有参数时走简单查询协议，一次可以跑多条语句；
    # op.execute() 走扩展协议，多语句会报
    # "cannot insert multiple commands into a prepared statement"。
    conn = op.get_bind().connection.dbapi_connection
    conn.execute(sql)


def upgrade() -> None:
    _run_script("001_baseline.sql")


def downgrade() -> None:
    raise NotImplementedError(
        "基线不做自动降级：重建库请用 `python scripts/init_db.py --drop`（或手工 drop database）")
