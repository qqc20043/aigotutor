"""pytest 配置：每次测试运行使用全新的独立临时库。

为什么必须"每次全新"：SQLAlchemy 的 create_all 只建表、不改表。
若复用上次运行的库文件（旧版 prefix 固定，会命中同一路径），模型新增字段
（如 submissions.judged_by）不会被补上，测试会以"用户名或密码错误"这类
误导性错误失败 —— 排查半天才发现是库陈旧。因此这里用 uuid 生成一次性库。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前设置环境变量
_tmpdir = Path(tempfile.mkdtemp(prefix=f"aigotutor-test-{uuid.uuid4().hex[:8]}-"))
os.environ["AIGOTUTOR_DB"] = str(_tmpdir / "test.db")
os.environ["AIGOTUTOR_PROVIDER"] = "mock"
os.environ["AIGOTUTOR_SECRET"] = "test-secret"
# 默认关闭限流：多个测试用例共享同一进程与同一客户端 IP，
# 累加请求会误触 429，让无关测试以"请求过于频繁"失败。
# 限流本身由 tests/test_ratelimit.py 显式开启并验证。
os.environ["AIGOTUTOR_RATE_LIMIT"] = "0"

atexit.register(lambda: shutil.rmtree(_tmpdir, ignore_errors=True))


@pytest.fixture(autouse=True)
async def _ensure_schema():
    """确保数据库已建表（幂等）。

    必要性：直接操作 session 的测试（如并发 upsert）不经过 client fixture，
    也就不会触发 lifespan 里的 init_db，会以
    "no such table: background_tasks" 失败 —— 这类错误极具误导性。
    放在 autouse fixture 里可一次性覆盖全部测试，且 init_db 是幂等的。
    """
    from app.db import init_db

    await init_db()


@pytest.fixture
def enable_rate_limit(monkeypatch):
    """为限流测试临时开启限流，并在结束后复位计数。"""
    from app.config import settings
    from app.middleware import reset_rate_limit

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    reset_rate_limit()
    yield
    reset_rate_limit()


@pytest.fixture
async def client():
    """httpx ASGI 客户端，配合 FastAPI lifespan 启动。"""
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest.fixture
async def auth_client(client):
    """已登录的客户端（全新用户，无学习数据）。"""
    import uuid

    username = f"u{uuid.uuid4().hex[:10]}"
    r = await client.post(
        "/api/auth/register",
        json={"username": username, "password": "test123456", "nickname": "测试"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
async def seeded_client(client):
    """带学习数据的客户端：登录种子用户 demo（12 标签 + 12 题 + 48 提交 + 画像）。

    注意必须用 demo 身份，而不是新注册用户 —— 种子数据都挂在 demo 名下，
    只有以 demo 登录才能验证掌握度画像、错题本、复习队列这些依赖历史数据的接口。
    """
    from scripts.seed import seed

    await seed(reset=False)
    r = await client.post("/api/auth/login", json={"username": "demo", "password": "demo123456"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return client
