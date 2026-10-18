"""性能索引：深分页复合索引 + 对账反查 + 幂等键清理

对应 `migrations/sql/002_perf_indexes.sql`，全部 `CREATE INDEX IF NOT EXISTS`，
所以对"已经手工建过这些索引"的库重复执行也安全（stamp 之后补齐版本号即可）。

Revision ID: 0002_perf_indexes
Revises: 0001_baseline
Create Date: 2026-10-18
"""
from __future__ import annotations

import pathlib

from alembic import op

revision = "0002_perf_indexes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"

INDEXES = (
    "ix_defects_project_keyset",
    "ix_defect_links_execution",
    "ix_defects_execution",
    "ix_idempotency_created",
)


def _run_script(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    conn = op.get_bind().connection.dbapi_connection
    conn.execute(sql)


def upgrade() -> None:
    _run_script("002_perf_indexes.sql")


def downgrade() -> None:
    for index in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index}")
