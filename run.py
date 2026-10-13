"""本地启动脚本：python run.py

Windows 事件循环的坑（实测踩到，勿轻改）：
    uvicorn 在 reload 或 workers>1 时会传 use_subprocess=True，此时 Windows
    上会选用 SelectorEventLoop；而该循环**不支持子进程**
    （create_subprocess_exec 直接抛 NotImplementedError）。
    判题沙箱正是靠子进程执行用户代码 —— 于是"默认启动方式 + 判题"必然 500。

    修法：显式指定 ProactorEventLoop（Windows 上唯一支持子进程的循环）。
    实测 Proactor 与 WatchFiles 热重载可共存，判题验收 18/18 通过。

    非 Windows 平台无需特殊处理（Selector 在 POSIX 上本就支持子进程）。
"""

from __future__ import annotations

import os
import sys

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "1") == "1"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    kwargs: dict[str, object] = {}
    if sys.platform == "win32" and reload:
        # 见模块 docstring：Windows + reload 必须钉住 Proactor，否则判题不可用
        kwargs["loop"] = "asyncio:ProactorEventLoop"

    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=reload, **kwargs)  # type: ignore[arg-type]
