"""B 档能力测试：Redis 接入（含优雅降级）与 LangGraph 编排。

守护两条核心契约：
  * **Redis 是可选增强**：未配置/不可用时，限流、缓存、写锁都必须自动
    回落进程内实现，功能不受影响（单机开发零外部依赖是项目定位）；
  * **编排语义不变**：从手写顺序调用迁到 LangGraph 后，进度上报、条件分支、
    降级汇总的行为必须与迁移前一致（否则是一次静默的行为回归）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.core.lock import distributed_lock, lock_info


# ==================================================== Redis 优雅降级
def test_redis_client_returns_none_when_unconfigured(monkeypatch):
    """未配置 REDIS_URL 时必须返回 None（而不是抛异常）。"""
    from app.core.redis_client import get_redis, reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    assert get_redis() is None
    reset_redis_cache()


def test_redis_info_reports_fallback_state(monkeypatch):
    """未配置时的运行态信息要说明"已降级"，不能假装可用。"""
    from app.core.redis_client import redis_info, reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    info = redis_info()
    assert info["configured"] is False
    assert info["available"] is False
    assert "进程内" in info["note"]
    reset_redis_cache()


def test_redis_url_is_redacted_in_info(monkeypatch):
    """带密码的连接串在运行态信息里必须脱敏（日志/接口不泄露密码）。"""
    from app.core.redis_client import _redact

    assert _redact("redis://:secret123@127.0.0.1:6379/0") == "redis://***@127.0.0.1:6379/0"
    assert _redact("redis://127.0.0.1:6379/0") == "redis://127.0.0.1:6379/0"


def test_rate_limiter_falls_back_to_local(monkeypatch):
    """Redis 不可用时限流必须回落到进程内，且行为正确。"""
    from app.core.redis_client import reset_redis_cache
    from app.middleware import RateLimiter

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    rl = RateLimiter(window_seconds=60)
    results = [rl.check("k", 2)[0] for _ in range(4)]
    assert results == [True, True, False, False]
    reset_redis_cache()


def test_cache_falls_back_to_local(monkeypatch):
    """Redis 不可用时缓存必须回落到进程内 LRU。"""
    from app.core.cache import cache_clear, cache_stats, get_cache, set_cache
    from app.core.redis_client import reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    cache_clear()
    set_cache("k1", {"v": 1}, ttl=30)
    assert get_cache("k1") == {"v": 1}
    assert cache_stats()["backend"] == "memory"
    cache_clear()
    reset_redis_cache()


@pytest.mark.asyncio
async def test_lock_falls_back_to_local(monkeypatch):
    """Redis 不可用时写锁必须回落到进程内锁，且仍能互斥。"""
    from app.core.redis_client import reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()

    order: list[str] = []

    async def worker(n: int) -> None:
        async with distributed_lock("t") as info:
            assert info["backend"] == "local"
            order.append(f"in-{n}")
            await asyncio.sleep(0.02)
            order.append(f"out-{n}")

    await asyncio.gather(worker(1), worker(2))
    # 互斥意味着不会有交叉执行
    assert order in (
        ["in-1", "out-1", "in-2", "out-2"],
        ["in-2", "out-2", "in-1", "out-1"],
    )
    reset_redis_cache()


@pytest.mark.asyncio
async def test_lock_info_reports_backend(monkeypatch):
    """锁的运行态信息要如实上报当前后端。"""
    from app.core.redis_client import reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    info = lock_info()
    assert info["backend"] == "local"
    assert "多 worker" in info["note"] or "进程内" in info["note"]
    reset_redis_cache()


# ==================================================== LangGraph 编排
@pytest.mark.asyncio
async def test_graph_engine_reported():
    """链路汇总必须标明引擎（便于确认真的走了 LangGraph）。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    task = TaskState(id="t-engine", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
    )
    assert summary["engine"] == "langgraph"


@pytest.mark.asyncio
async def test_graph_conditional_skips_recommend_without_candidates():
    """条件分支：没有候选题目时不执行推荐节点（也不应报失败）。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    task = TaskState(id="t-cond1", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[],  # 无候选
        tags=[{"name": "模拟", "id": "t1"}],
    )
    assert "recommend" not in summary["agents"]
    assert "roadmap" in summary["agents"]  # 但有标签 -> 路线图仍执行
    assert summary["failed"] == []


@pytest.mark.asyncio
async def test_graph_conditional_skips_roadmap_without_tags():
    """条件分支：没有标签时不画路线图。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    task = TaskState(id="t-cond2", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[],  # 无标签
    )
    assert "recommend" in summary["agents"]
    assert "roadmap" not in summary["agents"]


@pytest.mark.asyncio
async def test_graph_state_reducer_accumulates_all_nodes():
    """状态合并必须累加而非覆盖 —— 这是 LangGraph 最容易踩的坑。

    若 agents/degraded 用默认的"后写覆盖"语义，第二个节点会覆盖第一个的
    产出，汇总统计静默变少（不报错、只是数字不对，极难察觉）。
    """
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    task = TaskState(id="t-merge", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[{"name": "模拟", "id": "t1"}],
    )
    # 四个节点全部产出，一个都不能丢
    assert set(summary["agents"]) == {"judge", "mastery", "recommend", "roadmap"}
    assert summary["agent_count"] == 4


@pytest.mark.asyncio
async def test_graph_reports_progress_and_finishes():
    """进度上报与终态必须与迁移前一致（前端依赖 progress 事件画进度条）。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    task = TaskState(id="t-prog", user_id="u1", kind="agent_pipeline")
    await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[{"name": "模拟", "id": "t1"}],
    )
    progress_events = [e for e in task.events if e["event"] == "progress"]
    assert len(progress_events) >= 4  # 每个节点一次
    assert task.progress == 1.0


@pytest.mark.asyncio
async def test_graph_degrades_gracefully_when_llm_broken(monkeypatch):
    """全链路降级演练：所有 Agent 都失败时，图仍走完并汇总（不抛异常）。"""
    import httpx

    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    async def _boom(*args, **kwargs):
        raise httpx.ConnectError("mock 网络故障")

    # 让 provider 的 complete 直接抛异常 -> 触发 Agent 层降级
    from app.core import providers

    monkeypatch.setattr(providers.OpenAIProvider, "complete", _boom, raising=False)

    task = TaskState(id="t-degrade", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "openai", "model": "x", "api_key": "k"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[{"name": "模拟", "id": "t1"}],
    )
    # 降级成功则 failed 为空；降级也失败则进 failed —— 两者都不能让图崩掉
    assert set(summary["agents"]) == {"judge", "mastery", "recommend", "roadmap"}
    assert summary["failed"] == []  # 规则引擎兜底应当成功
    assert summary["degraded"]  # 但确实降级了
    assert summary["degraded_rate"] > 0


@pytest.mark.asyncio
async def test_graph_on_result_callback_invoked():
    """on_result 回调必须对每个节点都被调用（落库依赖它）。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    seen: list[str] = []

    async def _cb(name: str, result: dict) -> None:
        seen.append(name)

    task = TaskState(id="t-cb", user_id="u1", kind="agent_pipeline")
    await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[{"name": "模拟", "id": "t1"}],
        on_result=_cb,
    )
    assert set(seen) == {"judge", "mastery", "recommend", "roadmap"}


@pytest.mark.asyncio
async def test_graph_on_result_failure_does_not_break_pipeline():
    """落库回调失败不能拖垮链路（这是刻意设计的容错点）。"""
    from app.core.graph import run_practice_graph
    from app.core.tasks import TaskState

    async def _bad_cb(name: str, result: dict) -> None:
        raise RuntimeError("模拟落库失败")

    task = TaskState(id="t-cbfail", user_id="u1", kind="agent_pipeline")
    summary = await run_practice_graph(
        task,
        user_id="u1",
        config={"provider": "mock", "model": "mock-1"},
        submission={"verdict": "WA", "code": "print(0)"},
        problem={"id": "p1", "title": "t", "statement": "s", "difficulty_score": 50.0},
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[{"id": "p2", "title": "t2", "difficulty": "普及-", "difficulty_score": 45.0}],
        tags=[{"name": "模拟", "id": "t1"}],
        on_result=_bad_cb,
    )
    assert set(summary["agents"]) == {"judge", "mastery", "recommend", "roadmap"}
    assert task.progress == 1.0
