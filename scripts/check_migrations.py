"""验证 Alembic 迁移：从空库 upgrade 到 head，对比模型定义是否一致。

这是迁移体系的核心验收 —— 迁移脚本必须能独立重建出与模型一致的结构，
否则"迁移"只是装饰。

用法：
    python scripts/check_migrations.py
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def model_schema() -> dict[str, set[str]]:
    """从 SQLAlchemy 模型读出期望的表与列。"""
    from app.models import Base

    return {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}


def db_schema(db_path: Path) -> dict[str, set[str]]:
    """从实际数据库读出表与列（排除 alembic 自己的版本表）。"""
    con = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        out: dict[str, set[str]] = {}
        for t in tables:
            if t == "alembic_version":
                continue
            out[t] = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
        return out
    finally:
        con.close()


def run_alembic(args: list[str], db_path: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["AIGOTUTOR_DB"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("== Alembic 迁移验收 ==\n")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    versions = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    check("迁移脚本已生成", len(versions) >= 1, f"{len(versions)} 个")

    tmp = Path(tempfile.mkdtemp(prefix="aigotutor-mig-"))
    db = tmp / "mig.db"

    # 1) 空库 upgrade head
    code, out = run_alembic(["upgrade", "head"], db)
    check("空库 upgrade head", code == 0 and db.exists(), f"exit={code}")
    if code != 0:
        print(out[-2000:])

    if db.exists():
        expected = model_schema()
        actual = db_schema(db)

        missing_tables = set(expected) - set(actual)
        extra_tables = set(actual) - set(expected)
        check(
            "表集合一致",
            not missing_tables and not extra_tables,
            f"缺 {sorted(missing_tables) or '无'} / 多 {sorted(extra_tables) or '无'}",
        )

        mismatched: list[str] = []
        for t in set(expected) & set(actual):
            missing_cols = expected[t] - actual[t]
            if missing_cols:
                mismatched.append(f"{t}: 缺 {sorted(missing_cols)}")
        check(
            "列集合一致",
            not mismatched,
            "; ".join(mismatched) if mismatched else f"{len(expected)} 张表",
        )

        # 2) 幂等：再 upgrade 一次不应报错
        code2, _ = run_alembic(["upgrade", "head"], db)
        check("重复 upgrade 幂等", code2 == 0)

        # 3) current 能读出修订号
        code3, out3 = run_alembic(["current"], db)
        current_ok = code3 == 0 and bool(out3.strip())
        check(
            "current 可读", current_ok, out3.strip().splitlines()[-1][:60] if out3.strip() else ""
        )

        # 4) downgrade base 再 upgrade（验证可回滚）
        code4, _ = run_alembic(["downgrade", "base"], db)
        check("downgrade base 可回滚", code4 == 0, f"exit={code4}")
        if code4 == 0:
            after_down = db_schema(db)
            check("回滚后表已清空", len(after_down) == 0, f"剩 {len(after_down)} 张表")

            code5, _ = run_alembic(["upgrade", "head"], db)
            check("回滚后可重新升级", code5 == 0 and len(db_schema(db)) == len(expected))

    # 清理
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n== 结果: {'全部通过' if not failures else '失败 ' + ', '.join(failures)} ==")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
