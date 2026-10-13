"""Alembic 迁移环境。

关键点：数据库地址从 app.config.settings 读取，与运行时保持单一来源，
避免 alembic.ini 与应用配置各写一份导致迁移到错误的库。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证能 import app（alembic 从任意目录执行都能工作）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """把异步驱动地址转成同步驱动地址。

    Alembic 默认用同步引擎；运行时是 sqlite+aiosqlite，这里换成 sqlite。
    """
    url = f"sqlite:///{settings.DB_PATH.as_posix()}"
    # 支持切换到 MySQL/Postgres 时自动去异步前缀
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "").replace("+aiomysql", "")
    return url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 不支持大部分 ALTER，用 batch 模式重建表
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 的 ALTER 能力有限，batch 模式会"建新表->拷数据->换名"，
            # 使删列/改类型成为可能
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
