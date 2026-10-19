"""项目成员表 project_members（ABAC 的数据来源）

Revision ID: 0004_project_members
Revises: 0003_app_users
Create Date: 2026-10-19
"""
from __future__ import annotations

import pathlib

from alembic import op

revision = "0004_project_members"
down_revision = "0003_app_users"
branch_labels = None
depends_on = None

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"


def _run_script(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    conn = op.get_bind().connection.dbapi_connection
    conn.execute(sql)


def upgrade() -> None:
    _run_script("004_project_members.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_members")
