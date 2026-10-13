"""限流与请求体限制测试（批次 C / F10）。"""

from __future__ import annotations

import uuid

from app.middleware import (
    RateLimiter,
    limit_for_path,
)


# ========================================================== 限流器单元
def test_sliding_window_allows_up_to_limit():
    lim = RateLimiter(window_seconds=60.0)
    for i in range(5):
        ok, remaining = lim.check("k", 5)
        assert ok is True, f"第 {i + 1} 次应放行"
        assert remaining == 4 - i
    ok, remaining = lim.check("k", 5)
    assert ok is False
    assert remaining == 0
    assert lim.rejected == 1


def test_different_keys_are_independent():
    lim = RateLimiter(window_seconds=60.0)
    assert lim.check("a", 1)[0] is True
    assert lim.check("a", 1)[0] is False
    # 另一个 key 不受影响
    assert lim.check("b", 1)[0] is True


def test_window_expiry_frees_quota():
    """窗口过期后额度应恢复（用极小窗口验证，避免测试等待）。"""
    import time

    lim = RateLimiter(window_seconds=0.15)
    assert lim.check("k", 1)[0] is True
    assert lim.check("k", 1)[0] is False
    time.sleep(0.2)
    assert lim.check("k", 1)[0] is True


def test_sweep_removes_empty_buckets():
    """空桶应被清理，避免一次性 IP 把内存撑爆。"""
    import time

    lim = RateLimiter(window_seconds=0.1)
    for i in range(20):
        lim.check(f"ip:10.0.0.{i}", 5)
    assert lim.stats()["buckets"] == 20
    time.sleep(0.15)
    # 触发 sweep
    lim.check("ip:trigger", 5)
    assert lim.stats()["buckets"] < 20


def test_reset_clears_state():
    lim = RateLimiter(window_seconds=60.0)
    lim.check("k", 1)
    lim.check("k", 1)
    lim.reset()
    assert lim.stats()["buckets"] == 0
    assert lim.stats()["rejected"] == 0


# ========================================================== 路径匹配规则
def test_limit_for_path_matches_specific_rules():
    """更具体的规则应优先（最长前缀匹配）。"""
    assert limit_for_path("/api/auth/login") == 10
    assert limit_for_path("/api/auth/register") == 5
    assert limit_for_path("/api/agent/run") == 30
    assert limit_for_path("/api/agent/pipeline") == 15


def test_limit_for_path_prefix_match():
    """带 id 的路径应匹配到前缀规则。"""
    assert limit_for_path("/api/submissions/abc123") == 60
    assert limit_for_path("/api/problems/xyz/testcases") == 120


def test_health_and_docs_unlimited():
    assert limit_for_path("/health") is None
    assert limit_for_path("/docs") is None
    assert limit_for_path("/openapi.json") is None
    assert limit_for_path("/metrics") is None


def test_unknown_path_uses_global_default():
    """未显式配置的路径回退到全局默认（保护未知接口）。"""
    from app.config import settings

    assert limit_for_path("/api/some/new/endpoint") == settings.RATE_LIMIT_PER_MINUTE


def test_effective_limit_respects_global_switch(monkeypatch):
    """开关关闭时一律不限流；开启时按路由规则生效。"""
    from app.config import settings
    from app.middleware import effective_limit

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    assert effective_limit("/api/auth/login") is None

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    assert effective_limit("/api/auth/login") == 10
    assert effective_limit("/health") is None


def test_admin_paths_have_own_limit():
    assert limit_for_path("/api/admin/users") == 120


# ============================================================ 中间件行为
async def test_rate_limit_returns_429_with_headers(client, enable_rate_limit):
    """触发限流：应返回 429 且带 Retry-After 与配额头。"""
    limit = limit_for_path("/api/auth/login") or 10

    last = None
    for _ in range(limit + 3):
        last = await client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrongpass1"},
        )
        if last.status_code == 429:
            break

    assert last is not None and last.status_code == 429, "未触发限流"
    assert "retry-after" in {k.lower() for k in last.headers}
    assert last.headers.get("X-RateLimit-Remaining") == "0"
    assert "过于频繁" in last.json()["detail"]


async def test_rate_limit_headers_on_normal_response(client, enable_rate_limit):
    """正常响应也应带配额头，便于客户端自适应。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": f"rl{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    assert r.status_code == 201
    assert r.headers.get("X-RateLimit-Limit") is not None


async def test_request_body_too_large_rejected(client, enable_rate_limit):
    """超大请求体应被 413 拒绝，而不是解析后才拒绝。"""
    from app.config import settings

    huge = "x" * (settings.MAX_REQUEST_BYTES + 1024)
    r = await client.post(
        "/api/auth/login",
        json={"username": huge, "password": "secret12345"},
    )
    assert r.status_code == 413, r.text
    assert "过大" in r.json()["detail"]


async def test_health_not_rate_limited(client, enable_rate_limit):
    """健康检查不限流（监控高频轮询不应被拒）。"""
    for _ in range(50):
        r = await client.get("/health")
        assert r.status_code == 200


async def test_body_limit_applies_even_when_rate_limit_off(client):
    """请求体限制与限流是两件事：关掉限流后体积极限仍应生效。"""
    from app.config import settings

    huge = "x" * (settings.MAX_REQUEST_BYTES + 1024)
    r = await client.post("/api/auth/login", json={"username": huge, "password": "secret12345"})
    assert r.status_code == 413


async def test_limiter_stats_exposed(client):
    r = await client.get("/api/stats/global")
    assert r.status_code == 200
    assert "rate_limit" in r.json()
    assert "rejected" in r.json()["rate_limit"]
