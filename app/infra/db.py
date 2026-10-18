"""连接池与会话管理（PostgreSQL）。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# 本机绿色版 PG（端口 5433）；生产换成正式实例即可，代码不变
DATABASE_URL = os.environ.get(
    "TDP_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/tdp")

# 连接池参数：并发压测时最容易被忽略的一环。
# pool_size=20 + max_overflow=20 => 单进程最多 40 条连接；
# 1000 并发线程会排队等连接，这正是"应用层上限"的真实来源之一。
engine = create_engine(
    DATABASE_URL,
    pool_size=int(os.environ.get("TDP_POOL_SIZE", "20")),
    max_overflow=int(os.environ.get("TDP_POOL_OVERFLOW", "20")),
    pool_pre_ping=True,
    pool_timeout=30,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个事务一个会话；异常自动回滚。

    注意：**幂等键与业务写入必须在同一事务**——否则会出现
    "占了档期但幂等键没写"，客户端重试就会重复占用。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
