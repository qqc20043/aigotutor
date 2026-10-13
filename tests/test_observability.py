"""可观测性测试（批次 C / F21、F22）：trace id、结构化日志、Prometheus 指标。"""

from __future__ import annotations

import json
import logging
import uuid

from app.observability import (
    JsonFormatter,
    Metrics,
    _normalize_path,
    get_metrics,
    get_trace_id,
    new_trace_id,
    set_trace_id,
    set_user_id,
)


# ============================================================ trace id
def test_trace_id_propagation():
    tid = new_trace_id()
    set_trace_id(tid)
    assert get_trace_id() == tid


def test_new_trace_id_is_unique():
    ids = {new_trace_id() for _ in range(100)}
    assert len(ids) == 100


async def test_trace_id_in_response_header(client):
    """每个响应都应带 X-Trace-Id，便于用户报障时定位日志。"""
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Trace-Id")


async def test_incoming_trace_id_is_reused(client):
    """上游传入的 trace id 应被复用（跨服务链路追踪）。"""
    r = await client.get("/health", headers={"X-Trace-Id": "upstream-abc-123"})
    assert r.headers.get("X-Trace-Id") == "upstream-abc-123"


async def test_x_request_id_also_accepted(client):
    r = await client.get("/health", headers={"X-Request-Id": "req-xyz-789"})
    assert r.headers.get("X-Trace-Id") == "req-xyz-789"


# ============================================================ 结构化日志
def test_json_formatter_emits_structured_fields():
    set_trace_id("tid-001")
    set_user_id("u-42")
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.extra_fields = {"custom": 123}

    payload = json.loads(fmt.format(record))
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["trace_id"] == "tid-001"
    assert payload["user_id"] == "u-42"
    assert payload["custom"] == 123


def test_json_formatter_handles_exception():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(fmt.format(record))
    assert "ValueError" in payload["exc"]


def test_json_formatter_escapes_unicode_safely():
    """中文不应被转义成 \\uXXXX（日志可读性）。"""
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="中文日志",
        args=(),
        exc_info=None,
    )
    out = fmt.format(record)
    assert "中文日志" in out


# ================================================================ 指标
def test_metrics_http_observation():
    m = Metrics()
    m.observe_http("GET", "/api/problems", 200, 0.05)
    m.observe_http("GET", "/api/problems", 200, 0.07)
    m.observe_http("POST", "/api/submissions", 201, 1.2)

    assert m.http_requests[("GET", "/api/problems", 200)] == 2
    assert m.http_requests[("POST", "/api/submissions", 201)] == 1
    assert m.http_duration_count[("GET", "/api/problems")] == 2


def test_metrics_path_normalization_prevents_cardinality_explosion():
    """带 id 的路径必须归一，否则每个 id 都会生成一条时序。"""
    # 本项目 id 是 uuid4().hex（32 位十六进制）
    real_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert _normalize_path(f"/api/problems/{real_id}") == "/api/problems/{id}"
    # 纯数字 id
    assert _normalize_path("/api/submissions/12345") == "/api/submissions/{id}"
    # 子资源名必须保留（否则不同接口会混成一条时序）
    assert _normalize_path(f"/api/problems/{real_id}/testcases") == "/api/problems/{id}/testcases"
    # 集合路径本身不得被误伤（曾用长度启发式把 submissions 判成 id）
    assert _normalize_path("/api/submissions") == "/api/submissions"
    assert _normalize_path("/api/problems") == "/api/problems"
    assert _normalize_path("/api/review/queue") == "/api/review/queue"
    assert _normalize_path("/api/recommend/next") == "/api/recommend/next"
    assert _normalize_path("/api/users/me/model-config") == "/api/users/me/model-config"
    assert _normalize_path("/api/mastery/overview") == "/api/mastery/overview"


def test_normalize_path_keeps_short_fixed_segments():
    """短固定段（如 me、run）不能被当作 id 抹掉。"""
    assert _normalize_path("/api/auth/me") == "/api/auth/me"
    assert _normalize_path("/api/agent/run") == "/api/agent/run"
    assert _normalize_path("/health") == "/health"


def test_metrics_agent_recording():
    m = Metrics()
    m.record_agent("judge", "ok", tokens=100)
    m.record_agent("judge", "degraded", tokens=0)
    m.record_agent("judge", "ok", tokens=50)

    assert m.agent_calls[("judge", "ok")] == 2
    assert m.agent_calls[("judge", "degraded")] == 1
    assert m.agent_degraded["judge"] == 1
    assert m.llm_tokens["judge"] == 150


def test_metrics_judge_and_ratelimit():
    m = Metrics()
    m.record_judge("AC")
    m.record_judge("AC")
    m.record_judge("WA")
    m.record_rate_limited()

    assert m.judge_runs["AC"] == 2
    assert m.judge_runs["WA"] == 1
    assert m.rate_limited == 1


def test_prometheus_render_format():
    """输出必须是合法 Prometheus 文本格式。"""
    m = Metrics()
    real_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    m.observe_http("GET", f"/api/problems/{real_id}", 200, 0.05)
    m.record_agent("judge", "ok", tokens=100)
    m.record_agent("judge", "degraded")
    m.record_judge("AC")
    m.record_rate_limited()

    text = m.render()

    # 每个指标必须有 HELP 与 TYPE
    for metric in (
        "aigotutor_http_requests_total",
        "aigotutor_http_duration_seconds",
        "aigotutor_http_in_flight",
        "aigotutor_agent_calls_total",
        "aigotutor_agent_degraded_total",
        "aigotutor_llm_tokens_total",
        "aigotutor_judge_total",
        "aigotutor_rate_limited_total",
        "aigotutor_uptime_seconds",
    ):
        assert f"# HELP {metric}" in text, f"缺 HELP: {metric}"
        assert f"# TYPE {metric}" in text, f"缺 TYPE: {metric}"

    # 路径已归一
    assert "/api/problems/{id}" in text
    # 无未替换的模板残留
    assert "${" not in text


def test_prometheus_label_escaping():
    """label 值里的引号与反斜杠必须转义，否则解析失败。"""
    m = Metrics()
    m.record_agent('we"ird\\agent', "ok")
    text = m.render()
    assert '\\"' in text
    assert "\\\\" in text


def test_prometheus_always_emits_metadata_even_without_samples():
    """空指标也必须输出 HELP/TYPE。

    踩过的坑：早期实现"无样本就跳过整个指标"，导致服务刚启动、还没产生
    降级/token 数据时，/metrics 里根本没有这些指标的元信息 ——
    监控系统会报"未知指标"，告警规则无法生效。
    """
    m = Metrics()  # 全新实例，所有计数器为空
    text = m.render()

    for metric in (
        "aigotutor_http_requests_total",
        "aigotutor_http_duration_seconds",
        "aigotutor_agent_calls_total",
        "aigotutor_agent_degraded_total",
        "aigotutor_llm_tokens_total",
        "aigotutor_judge_total",
        "aigotutor_rate_limited_total",
        "aigotutor_http_in_flight",
        "aigotutor_uptime_seconds",
    ):
        assert f"# HELP {metric}" in text, f"空指标缺少 HELP: {metric}"
        assert f"# TYPE {metric}" in text, f"空指标缺少 TYPE: {metric}"


def test_prometheus_metadata_appears_once():
    """每个指标的 HELP/TYPE 只能出现一次（重复会导致 Prometheus 解析报错）。"""
    m = Metrics()
    m.observe_http("GET", "/x", 200, 0.01)
    m.record_judge("AC")
    text = m.render()

    for metric in ("aigotutor_http_requests_total", "aigotutor_judge_total"):
        assert text.count(f"# HELP {metric}") == 1, f"{metric} 的 HELP 重复"
        assert text.count(f"# TYPE {metric}") == 1, f"{metric} 的 TYPE 重复"


def test_metrics_reset():
    m = Metrics()
    m.observe_http("GET", "/x", 200, 0.1)
    m.record_judge("AC")
    m.reset()
    assert sum(m.http_requests.values()) == 0
    assert not m.judge_runs


# ============================================================ HTTP 端点
async def test_metrics_endpoint_available(client):
    """未认证也能抓取指标（Prometheus 通常不带业务 token）。"""
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "aigotutor_http_requests_total" in r.text


async def test_metrics_not_in_openapi(client):
    """指标端点不应出现在公开 API 文档里。"""
    r = await client.get("/openapi.json")
    assert "/metrics" not in r.json()["paths"]


async def test_metrics_accumulate_across_requests(client):
    """请求应被计数：抓两次 /health，指标里的请求数应增加。"""
    before = get_metrics().http_requests.get(("GET", "/health", 200), 0)
    await client.get("/health")
    await client.get("/health")
    after = get_metrics().http_requests.get(("GET", "/health", 200), 0)
    assert after >= before + 2


async def test_agent_metric_recorded_via_log_agent_run(client):
    """通过真实接口跑一次 Agent，指标里应出现调用计数。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": f"obs{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    before = dict(get_metrics().agent_calls)
    r2 = await client.post(
        "/api/agent/run",
        json={"agent": "judge", "payload": {"verdict": "WA", "code": "x"}},
        headers=h,
    )
    assert r2.status_code == 200

    after = get_metrics().agent_calls
    key = ("judge", "ok")
    assert after.get(key, 0) >= before.get(key, 0) + 1


async def test_stats_global_exposes_metrics(client):
    r = await client.get("/api/stats/global")
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body
    assert "http_requests" in body["metrics"]
    assert "uptime_seconds" in body["metrics"]


async def test_trace_id_present_even_when_rate_limited(client, enable_rate_limit):
    """被限流的响应也应有 trace id（否则用户报障无从定位）。"""
    from app.middleware import limit_for_path

    limit = limit_for_path("/api/auth/login") or 10
    last = None
    for _ in range(limit + 3):
        last = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "wrongpass1"}
        )
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429
    assert last.headers.get("X-Trace-Id")
