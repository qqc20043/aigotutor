"""轻量级 schema 自愈：启动时对比模型与实际库结构，自动补齐缺失的列与表。

为什么需要它（比 Alembic 更前置的一道防线）：
  SQLAlchemy 的 create_all 只建表、不改表。模型加字段后，老库不会自动补列，
  于是运行时炸出 "no such column" —— 开发期必须手动删库，部署时更是事故。
  实测踩到过：新增 submissions.judged_by 后，真实服务 500 而测试全绿
  （测试用全新临时库，掩盖了问题）。

本模块只做"加法"：
  * 缺表 -> CREATE TABLE
  * 缺列 -> ALTER TABLE ADD COLUMN（SQLite 支持，且能带默认值）
  绝不删列、不改类型 —— 破坏性变更交给 Alembic 显式迁移，避免误删数据。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger("aigotutor.schema")


def _sqlite_type(column: Any) -> str:
    """把 SQLAlchemy 列类型映射成 SQLite 类型名。"""
    try:
        return column.type.compile(dialect=__import__("sqlalchemy").dialects.sqlite.dialect())
    except Exception:
        return "TEXT"


def _default_clause(column: Any) -> str:
    """为 NOT NULL 新列生成 DEFAULT，避免存量行违反约束。

    坑：SQLAlchemy 的 ColumnDefault.arg 对 `default=utcnow` 这类会被包装成
    需要 ExecutionContext 参数的可调用对象，直接 arg() 会抛
    "utcnow() missing 1 required positional argument: 'ctx'"。
    因此这里只接受"能安全取值的静态默认值"，动态默认值退化为按类型给的常量。
    """
    if column.default is not None:
        arg = getattr(column.default, "arg", None)
        if arg is not None and not getattr(column.default, "is_callable", False):
            value = arg
            if isinstance(value, bool):
                return f" DEFAULT {1 if value else 0}"
            if isinstance(value, (int, float)):
                return f" DEFAULT {value}"
            if isinstance(value, str):
                safe = value.replace("'", "''")
                return f" DEFAULT '{safe}'"

    if not column.nullable:
        t = _sqlite_type(column).upper()
        if "BOOL" in t:
            return " DEFAULT 0"
        if any(
            k in t for k in ("INT", "FLOAT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "DATE", "TIME")
        ):
            return " DEFAULT 0"
        return " DEFAULT ''"
    return ""


async def sync_schema(conn: AsyncConnection, metadata: Any) -> dict[str, list[str]]:
    """对比 metadata 与实际库，补齐缺失的表与列。返回变更清单。"""
    report: dict[str, list[str]] = {"created_tables": [], "added_columns": []}

    def _inspect(sync_conn) -> tuple[list[str], dict[str, set[str]]]:
        insp = inspect(sync_conn)
        tables = insp.get_table_names()
        cols = {t: {c["name"] for c in insp.get_columns(t)} for t in tables}
        return tables, cols

    existing_tables, existing_cols = await conn.run_sync(_inspect)

    for table in metadata.sorted_tables:
        name = table.name
        if name not in existing_tables:
            # 交给 create_all 统一建（这里只登记）
            report["created_tables"].append(name)
            continue
        have = existing_cols.get(name, set())
        for column in table.columns:
            if column.name in have:
                continue
            ddl = (
                f'ALTER TABLE "{name}" ADD COLUMN "{column.name}" '
                f"{_sqlite_type(column)}{_default_clause(column)}"
            )
            try:
                await conn.execute(text(ddl))
                report["added_columns"].append(f"{name}.{column.name}")
                logger.info("schema 自愈：新增列 %s.%s", name, column.name)
            except Exception as e:  # 加列失败不应阻断启动
                logger.warning("schema 自愈失败 %s.%s: %s", name, column.name, e)

    return report
