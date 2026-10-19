"""认证账号表 app_users

DDL 在 `migrations/sql/003_app_users.sql`（和前面两个迁移一样的约定：一个文件一件事）。

Revision ID: 0003_app_users
Revises: 0002_perf_indexes
Create Date: 2026-10-19
"""
from __future__ import annotations

import pathlib

from alembic import op

revision = "0003_app_users"
down_revision = "0002_perf_indexes"
branch_labels = None
depends_on = None

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"


def _run_script(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    # 走 DBAPI 连接（无参数 → 简单查询协议），一次可以执行多条语句
    conn = op.get_bind().connection.dbapi_connection
    conn.execute(sql)


def upgrade() -> None:
    _run_script("003_app_users.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_users")
