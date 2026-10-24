"""MCP Server 端到端冒烟：stdio 握手 + 列工具 + 调一个工具。

用法（在仓库根目录）：
    python scripts/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"tools ({len(names)}):")
            for n in names:
                print(" -", n)
            res = await session.call_tool("aigotutor_mastery_profile", {})
            print("call mastery_profile ->", res.content[0].text[:160])
            return 0 if len(names) >= 11 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
