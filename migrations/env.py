"""Alembic 运行环境。

和标准模板的差别只有一处：**不做 autogenerate**（`target_metadata = None`）。
本项目的表都是用 `migrations/sql/*.sql` 手写的，为了让 DDL 既能被 `psql -f` 直接跑、
又能被 `alembic upgrade` 按版本推进，迁移脚本只做"读文件 → 执行"。
"""
from __future__ import annotations

import os
import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

config = context.config

# 用 Config() 在代码里构造时 config_file_name 是 None，跳过日志配置即可
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# URL 优先级：调用方显式设置的 sqlalchemy.url > TDP_DATABASE_URL > 应用里的默认值。
# 最后这层兜底是为了让 `alembic current` 在本地裸跑也能用（默认指向 127.0.0.1:5433/tdp，
# 和 dev.ps1 一致）；CI / 容器里都显式给了环境变量，走不到这里。
url = config.get_main_option("sqlalchemy.url") or os.environ.get("TDP_DATABASE_URL")
if not url:
    try:
        from app.infra.db import DATABASE_URL as url  # noqa: F811
    except Exception:                       # noqa: BLE001 - 导入失败就保持 None
        url = None
if not url:
    raise RuntimeError(
        "Alembic 需要数据库地址：设置环境变量 TDP_DATABASE_URL，"
        "或调用方 config.set_main_option('sqlalchemy.url', ...)")
config.set_main_option("sqlalchemy.url", url)

target_metadata = None          # 手写 SQL，没有 ORM 模型可对比

SQL_DIR = BASE / "migrations" / "sql"


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=False)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=False)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
