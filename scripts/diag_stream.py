"""诊断：验证 token 级流式接口真实产出事件。"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmp = Path(tempfile.mkdtemp(prefix="aigotutor-stream-"))
os.environ["AIGOTUTOR_DB"] = str(tmp / "test.db")
os.environ["AIGOTUTOR_PROVIDER"] = "mock"
os.environ["AIGOTUTOR_SECRET"] = "stream-secret"

import httpx  # noqa: E402

from app.main import app  # noqa: E402


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            r = await c.post(
                "/api/auth/register",
                json={"username": "streamer", "password": "stream123456"},
            )
            c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

            counts: dict[str, int] = {}
            first_token_at = None
            fields: list[str] = []
            tokens_text: list[str] = []
            done_result = None

            async with c.stream(
                "POST",
                "/api/agent/stream",
                json={
                    "agent": "judge",
                    "payload": {"verdict": "TLE", "code": "for i in range(10**9): pass"},
                },
            ) as resp:
                print(
                    "status:", resp.status_code, "| content-type:", resp.headers.get("content-type")
                )
                async for line in resp.aiter_lines():
                    if not line.startswith("event:"):
                        continue
                    evt = line.split(":", 1)[1].strip()
                    counts[evt] = counts.get(evt, 0) + 1
                    if evt == "token" and first_token_at is None:
                        first_token_at = counts[evt]

            # 再用带 data 的完整读取验证字段事件
            async with c.stream(
                "POST",
                "/api/agent/stream",
                json={
                    "agent": "recommend",
                    "payload": {
                        "candidates": [
                            {
                                "problem_id": "p1",
                                "title": "A",
                                "difficulty": "普及-",
                                "difficulty_score": 45.0,
                                "tags": ["排序"],
                            },
                        ],
                        "mastery": {"排序": 30.0},
                        "recent_problem_ids": [],
                        "target_score": 55.0,
                    },
                },
            ) as resp:
                cur = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        cur = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and cur:
                        data = json.loads(line[5:].strip())
                        if cur == "field":
                            fields.append(data.get("key"))
                        elif cur == "token":
                            tokens_text.append(data.get("text", ""))
                        elif cur == "done":
                            done_result = data.get("result")

            print("事件统计:", counts)
            print("field 事件字段:", fields)
            print("token 事件数:", counts.get("token", 0))
            if done_result:
                print("done.status:", done_result.get("status"))
                print("done.data.problem_id:", (done_result.get("data") or {}).get("problem_id"))
                print("done.tokens:", done_result.get("tokens"))


asyncio.run(main())
