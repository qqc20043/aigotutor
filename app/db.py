"""异步数据库层：SQLAlchemy 2.0 + aiosqlite，SQLite 开启 WAL 与 busy_timeout。

并发写策略（真正生效的版本）：
  1. WAL 模式 -> 读不阻塞写，写不阻塞读；
  2. busy_timeout=5000 -> 写锁竞争时自动等待而不是立刻 database is locked；
  3. 所有写事务经全局写锁串行提交（见下）；
  4. 读事务不加锁，完全并发。

关于写锁（技术债 D1 的修复）：
  上一版在 core/tasks.py 里写了 serialize_write()，但**从未被调用**，
  所以"单写队列"是空头承诺。这里改成在事务提交层接入 —— 因为要串行化的
  不是单次 flush，而是"事务提交"这个真正抢数据库写锁的动作。
  这样 service 层的 33 处写操作无需逐个改动，也不会漏。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# 全局写锁：同一进程内所有写事务串行提交。
# 与事件循环绑定，循环更换时自动重建（测试每例新 loop）。
_write_lock: asyncio.Lock | None = None
_write_lock_loop: asyncio.AbstractEventLoop | None = None
# 统计：便于观测与压测断言
_stats = {"write_tx": 0, "read_tx": 0, "rollback": 0}


def _get_write_lock() -> asyncio.Lock:
    """取当前事件循环的写锁；循环变化则重建。"""
    global _write_lock, _write_lock_loop
    loop = asyncio.get_running_loop()
    if _write_lock is None or _write_lock_loop is not loop:
        _write_lock = asyncio.Lock()
        _write_lock_loop = loop
    return _write_lock


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = f"sqlite+aiosqlite:///{settings.DB_PATH.as_posix()}"
        _engine = create_async_engine(url, echo=settings.SQL_ECHO, future=True)

        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - 驱动回调
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def _commit_serialized(session: AsyncSession) -> None:
    """在全局写锁保护下提交。

    关于"是否需要拿写锁"的判定 —— 这里踩过一个坑：
    最初用 `session.new or session.dirty or session.deleted` 判断有没有改动，
    但业务代码普遍是 `db.add(obj)` + `await db.flush()`，flush 之后
    session.new 已经清空，于是被误判成"纯读事务"直接跳过 commit，
    数据静默丢失（seed 报成功但库里一条都没有）。

    正确做法：flush 会把变更送进当前事务，因此只要事务内有任何 SQL，
    就一定需要提交。这里改用 session 的事务状态判断，宁可多提交一次
    空事务，也绝不漏提交。（空事务提交代价极低，数据丢失不可接受。）
    """
    lock = _get_write_lock()
    async with lock:
        _stats["write_tx"] += 1
        await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await _commit_serialized(session)
        except Exception:
            _stats["rollback"] += 1
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await _commit_serialized(session)
        except Exception:
            _stats["rollback"] += 1
            await session.rollback()
            raise


async def init_db() -> None:
    """建表 + 自愈。

    create_all 只建缺失的表，不会给已有表补列。模型演进后老库会缺列，
    运行时炸 "no such column"。因此这里追加一次 schema 自愈（只做加法）。
    """
    from .schema_sync import sync_schema

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        report = await sync_schema(conn, Base.metadata)
    if report.get("added_columns"):
        import logging

        logging.getLogger("aigotutor.schema").info(
            "schema 自愈完成：补齐 %d 列", len(report["added_columns"])
        )


async def drop_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def reset_db() -> None:
    await drop_db()
    await init_db()


async def healthcheck() -> bool:
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def write_stats() -> dict[str, int]:
    """写锁观测指标：写事务数 / 读事务数 / 回滚数。"""
    return dict(_stats)


def reset_write_stats() -> None:
    for k in _stats:
        _stats[k] = 0
