"""schema 自愈与工具链探测测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.judge.base import probe_language


# ======================================================= 工具链版本探测
def test_probe_python_available():
    ok, msg = probe_language("python")
    # 本机可能没有 python，但只要返回 ok 就不该有告警
    assert isinstance(ok, bool)
    if ok:
        assert msg == ""


def test_probe_unsupported_language():
    ok, msg = probe_language("brainfuck")
    assert ok is False
    assert "不支持" in msg


def test_probe_warns_only_on_real_mismatch(monkeypatch):
    """同版本必须不告警（曾因正则匹配不到 `javac 17` 而误报）。"""
    import subprocess as _sp

    class FakeResult:
        def __init__(self, text: str) -> None:
            self.stdout = text
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, **kw):
        if cmd[0].endswith("javac") or cmd[0] == "javac":
            return FakeResult("javac 17.0.14")
        return FakeResult('openjdk version "17.0.14" 2025-01-21 LTS')

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(_sp, "run", fake_run)

    ok, msg = probe_language("java")
    assert ok is True
    assert msg == "", f"同版本不该告警，得到: {msg}"


def test_probe_detects_major_mismatch(monkeypatch):
    """真错配必须告警：javac 17 配 java 1.8。"""
    import subprocess as _sp

    class FakeResult:
        def __init__(self, text: str) -> None:
            self.stdout = text
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, **kw):
        if cmd[0].endswith("javac") or cmd[0] == "javac":
            return FakeResult("javac 17.0.14")
        return FakeResult('java version "1.8.0_341"')

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(_sp, "run", fake_run)

    ok, msg = probe_language("java")
    assert ok is True
    assert "不一致" in msg


# ========================================================== schema 自愈
async def test_schema_self_heal_adds_missing_column():
    """模拟老库缺列：自愈应补上，而不是让运行时炸 no such column。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.schema_sync import sync_schema

    db_path = Path(__file__).parent / "_selfheal.db"
    if db_path.exists():
        db_path.unlink()

    # 手工建一个"缺列"的旧表
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, username VARCHAR(64), "
        "password_hash VARCHAR(200), nickname VARCHAR(64))"
    )
    con.commit()
    con.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    try:
        from app.models import Base

        async with engine.begin() as conn:
            # 只对已存在的表补列（不建新表，聚焦验证 ADD COLUMN 路径）
            report = await sync_schema(conn, Base.metadata)

        assert any(c.startswith("users.") for c in report["added_columns"]), report

        # 确认新列真的存在
        con = sqlite3.connect(db_path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        con.close()
        assert "level" in cols
        assert "daily_goal" in cols
    finally:
        await engine.dispose()
        if db_path.exists():
            db_path.unlink()


async def test_schema_self_heal_is_idempotent():
    """重复执行不应报错，也不应重复加列。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base
    from app.schema_sync import sync_schema

    db_path = Path(__file__).parent / "_selfheal2.db"
    if db_path.exists():
        db_path.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            first = await sync_schema(conn, Base.metadata)
        assert first["added_columns"] == []

        async with engine.begin() as conn:
            second = await sync_schema(conn, Base.metadata)
        assert second["added_columns"] == []
        assert second["created_tables"] == []
    finally:
        await engine.dispose()
        if db_path.exists():
            db_path.unlink()


def test_init_db_is_callable():
    """init_db 现在包含自愈逻辑，必须仍能正常工作。"""
    import asyncio

    from app.db import init_db

    asyncio.run(init_db())
