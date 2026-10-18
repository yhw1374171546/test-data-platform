"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

import pathlib

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

# 迁移脚本只做"读 sql → 执行"；DDL 的单一来源在 migrations/sql/
SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"


def _run_script(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    # 用 DBAPI 连接执行：psycopg3 在没有参数时走简单查询协议，一次可以跑多条语句；
    # 而 op.execute() 走扩展协议，多语句会报
    # "cannot insert multiple commands into a prepared statement"。
    conn = op.get_bind().connection.dbapi_connection
    conn.execute(sql)


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
