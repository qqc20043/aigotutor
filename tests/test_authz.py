"""越权（IDOR）修复的回归测试 + 密钥静态加密 + CORS/metrics 门控。

来源：2026-10-24 代码审查 PoC —— 任务详情/取消/SSE 只验证登录不验证属主、
匿名可订阅任意任务事件流、任意登录用户可覆写任意题目的用例（含隐藏
期望输出，等于能伪造判题结果）。全部已 PoC 复现后修复，这里钉死回归。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import settings
from app.db import session_scope
from app.models import Problem, User
from app.security import decrypt_secret, encrypt_secret


@pytest.fixture
async def two_users(client):
    """两个互不相干的登录用户，返回 (client, tokenA, tokenB)。"""

    async def _register(c, name):
        r = await c.post(
            "/api/auth/register",
            json={"username": name, "password": "test123456", "nickname": name},
        )
        assert r.status_code == 201, r.text
        return r.json()["access_token"]

    ta = await _register(client, f"aura{uuid.uuid4().hex[:8]}")
    tb = await _register(client, f"burb{uuid.uuid4().hex[:8]}")
    return client, ta, tb


async def _make_task(client, token, problem_id: str) -> str:
    r = await client.post(
        "/api/agent/pipeline",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "submission": {
                "problem_id": problem_id,
                "code": "print(0)\n",
                "verdict": "WA",
                "passed_cases": 1,
                "total_cases": 4,
            },
            "include_roadmap": True,
        },
    )
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


async def _make_problem(client, token, title: str) -> str:
    r = await client.post(
        "/api/problems",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title, "statement": "statement for authz test", "tags": ["模拟"]},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _promote(user_id: str) -> None:
    async with session_scope() as db:
        u = await db.get(User, user_id)
        u.role = "admin"  # is_admin 由 role 派生（deps.CurrentUser）


# ==================================================== 任务接口属主校验
async def test_task_endpoints_reject_other_user(two_users):
    """B 不能读/取消/等待/订阅 A 的任务（修复前全部 200）。"""
    client, ta, tb = two_users
    pid = await _make_problem(client, ta, "A的任务题")
    tid = await _make_task(client, ta, pid)

    # 本人可读
    r = await client.get(f"/api/tasks/{tid}", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200, r.text

    # B 读 A 的任务详情 -> 404
    r = await client.get(f"/api/tasks/{tid}", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404, "跨用户读任务详情必须 404"

    # B 取消 A 的任务 -> 404
    r = await client.post(f"/api/tasks/{tid}/cancel", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404, "跨用户取消任务必须 404"

    # B 等待 A 的任务 -> 404
    r = await client.post(f"/api/tasks/{tid}/wait", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404, "跨用户等待任务必须 404"

    # B 订阅 A 的事件流 -> 404；匿名 -> 401（修复前匿名 200 直接拿到事件）
    r = await client.get(
        "/api/agent/stream", params={"task_id": tid}, headers={"Authorization": f"Bearer {tb}"}
    )
    assert r.status_code == 404, "跨用户订阅任务 SSE 必须 404"
    r = await client.get("/api/agent/stream", params={"task_id": tid})
    assert r.status_code == 401, "匿名订阅任务 SSE 必须 401"

    # 本人订阅正常
    r = await client.get(
        "/api/agent/stream", params={"task_id": tid}, headers={"Authorization": f"Bearer {ta}"}
    )
    assert r.status_code == 200


async def test_task_wait_timeout_capped(two_users):
    """wait 的 timeout 有上界（修复前可传 1e6 长占 worker）。"""
    client, ta, _ = two_users
    r = await client.post(
        "/api/tasks/whatever/wait",
        params={"timeout": 100000},
        headers={"Authorization": f"Bearer {ta}"},
    )
    assert r.status_code == 422, "超上限的 timeout 必须被查询参数校验拒绝"


# ==================================================== 题库用例写权限
async def test_testcase_write_requires_creator_or_admin(two_users):
    """B 不能改 A 的题目用例；A（创建者）和管理员可以。"""
    client, ta, tb = two_users
    pid = await _make_problem(client, ta, "A的私有题")
    cases = {"replace": True, "cases": [{"input": "1\n", "output": "2\n", "hidden": True}]}

    r = await client.put(
        f"/api/problems/{pid}/testcases", json=cases, headers={"Authorization": f"Bearer {tb}"}
    )
    assert r.status_code == 404, "非创建者覆写用例必须 404（修复前 200，可伪造判题结果）"

    r = await client.put(
        f"/api/problems/{pid}/testcases", json=cases, headers={"Authorization": f"Bearer {ta}"}
    )
    assert r.status_code == 200, r.text

    # B 删除 A 的用例 -> 404
    r = await client.get(
        f"/api/problems/{pid}/testcases", headers={"Authorization": f"Bearer {ta}"}
    )
    case_id = r.json()["items"][0]["id"]
    r = await client.delete(
        f"/api/problems/{pid}/testcases/{case_id}", headers={"Authorization": f"Bearer {tb}"}
    )
    assert r.status_code == 404, "非创建者删用例必须 404"

    # 管理员可以改（含 created_by 为空的种子/导入题）
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tb}"})
    await _promote(me.json()["id"])
    r = await client.put(
        f"/api/problems/{pid}/testcases", json=cases, headers={"Authorization": f"Bearer {tb}"}
    )
    assert r.status_code == 200, "管理员必须可以治理任意题目用例"


async def test_legacy_problem_without_creator_admin_only(two_users):
    """存量题（created_by 为空）只有管理员能改用例。"""
    client, ta, _tb = two_users
    pid = await _make_problem(client, ta, "旧数据题")
    async with session_scope() as db:
        p = await db.get(Problem, pid)
        p.created_by = ""  # 模拟种子/导入的存量行

    cases = {"replace": True, "cases": [{"input": "1\n", "output": "2\n"}]}
    r = await client.put(
        f"/api/problems/{pid}/testcases", json=cases, headers={"Authorization": f"Bearer {ta}"}
    )
    assert r.status_code == 404, "存量题（无归属）对普通用户（含历史创建者）应视为不可改"

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ta}"})
    await _promote(me.json()["id"])
    r = await client.put(
        f"/api/problems/{pid}/testcases", json=cases, headers={"Authorization": f"Bearer {ta}"}
    )
    assert r.status_code == 200


# ==================================================== API Key 静态加密
async def test_api_key_encrypted_at_rest(two_users):
    """用户 API Key 落库必须是密文，读出/脱敏是明文口径。"""
    client, ta, _ = two_users
    plain = "sk-authz-test-1234567890abcd"
    r = await client.put(
        "/api/users/me/model-config",
        json={"provider": "openai", "model": "m", "base_url": "https://x/v1", "api_key": plain},
        headers={"Authorization": f"Bearer {ta}"},
    )
    assert r.status_code == 200, r.text
    masked = r.json()["api_key_masked"]
    assert masked.startswith("sk-a"), f"脱敏前缀应来自明文而非密文: {masked}"

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ta}"})
    uid = me.json()["id"]
    from sqlalchemy import select as sa_select

    from app.models import UserModelConfig

    async with session_scope() as db:
        cfg = (
            await db.execute(sa_select(UserModelConfig).where(UserModelConfig.user_id == uid))
        ).scalar_one()
        stored = cfg.api_key_ref
    assert stored != plain, "API Key 不得明文落库"
    assert stored.startswith("gAAA"), f"应为 Fernet 密文: {stored[:16]}..."
    assert decrypt_secret(stored) == plain, "解密必须还原明文"


def test_secret_roundtrip_and_legacy_plaintext():
    assert decrypt_secret(encrypt_secret("sk-xyz-9876543210")) == "sk-xyz-9876543210"
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""
    # 存量明文行：解密失败原样返回（兼容）
    assert decrypt_secret("sk-legacy-plaintext-row") == "sk-legacy-plaintext-row"


# ==================================================== CORS 收敛与 /metrics 门控
async def test_cors_not_reflecting_arbitrary_origin(client):
    """默认配置下任意 Origin 不得被回显放行（修复前 * + credentials 等效全开）。"""
    r = await client.get(
        "/api/problems", headers={"Origin": "https://evil.example"}, params={"page": 1}
    )
    acao = r.headers.get("access-control-allow-origin")
    assert acao != "https://evil.example", "跨源响应头不得回显未配置的 Origin"


async def test_metrics_loopback_allowed_without_token(client):
    """本机（测试客户端）默认可抓 /metrics。"""
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "aigotutor" in r.text.lower() or r.text.strip()


async def test_metrics_token_gating(client, monkeypatch):
    """配置 METRICS_TOKEN 后：无 token 404、错 token 404、对 token 200（header 或查询参数）。"""
    monkeypatch.setattr(settings, "METRICS_TOKEN", "prom-secret")
    assert (await client.get("/metrics")).status_code == 404
    assert (await client.get("/metrics", params={"token": "wrong"})).status_code == 404
    assert (await client.get("/metrics", params={"token": "prom-secret"})).status_code == 200
    r = await client.get("/metrics", headers={"Authorization": "Bearer prom-secret"})
    assert r.status_code == 200


async def test_metrics_rejects_non_loopback_without_token(client, monkeypatch):
    """未配置 token 时非本机来源 404。"""
    # httpx ASGITransport 的 client 地址可注入：构造一个外部地址的请求
    transport = httpx.ASGITransport(
        app=__import__("app.main", fromlist=["app"]).app, client=("203.0.113.9", 55555)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/metrics")
    assert r.status_code == 404, "外网来源在未配置 token 时不得抓取 /metrics"


# ==================================================== SPA 缓存策略
async def test_spa_html_not_cached(client):
    """index.html 必须发 no-cache（重建 dist 后旧缓存会导致页面 chunk 404，
    登录成功但路由跳转静默失败——2026-10-24 实测复现）。"""
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
