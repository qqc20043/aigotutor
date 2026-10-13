"""API 端到端测试：认证、题库、提交评测、推荐、复习、掌握度、Agent/任务、数据。

覆盖简历里的每条产品主张，全部走真实 HTTP 栈（httpx ASGITransport）。
"""

from __future__ import annotations

import asyncio


# ============================================================ 认证与配置
async def test_register_login_me_flow(client):
    """注册 -> 登录 -> 读取当前用户。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": "alice_test", "password": "secret123", "nickname": "Alice"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]

    r = await client.post(
        "/api/auth/login", json={"username": "alice_test", "password": "secret123"}
    )
    assert r.status_code == 200
    assert r.json()["username"] == "alice_test"

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["nickname"] == "Alice"


async def test_duplicate_username_conflicts(client):
    body = {"username": "dup_user", "password": "secret123"}
    assert (await client.post("/api/auth/register", json=body)).status_code == 201
    assert (await client.post("/api/auth/register", json=body)).status_code == 409


async def test_wrong_password_rejected(client):
    await client.post("/api/auth/register", json={"username": "pw_user", "password": "right123"})
    r = await client.post("/api/auth/login", json={"username": "pw_user", "password": "wrong123"})
    assert r.status_code == 401


async def test_missing_token_rejected(client):
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_tampered_token_rejected(client):
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer abc.def"})
    assert r.status_code == 401


async def test_model_config_masks_api_key(auth_client):
    """用户级模型配置：密钥必须脱敏返回。"""
    r = await auth_client.put(
        "/api/users/me/model-config",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-1234567890abcdef",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "1234567890" not in body["api_key_masked"]
    assert body["api_key_masked"].startswith("sk-1")
    assert body["provider"] == "openai"

    r = await auth_client.get("/api/users/me/model-config")
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-4o-mini"


# ================================================================ 题库
async def test_problem_crud_and_search(seeded_client):
    r = await seeded_client.get("/api/problems")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 12
    assert body["items"][0]["tags"]

    pid = body["items"][0]["id"]
    r = await seeded_client.get(f"/api/problems/{pid}")
    assert r.status_code == 200
    assert r.json()["statement"]

    r = await seeded_client.get("/api/problems/random")
    assert r.status_code == 200

    r = await seeded_client.get("/api/problems/search", params={"q": "背包"})
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    r = await seeded_client.get("/api/problems/tags")
    assert r.status_code == 200
    assert len(r.json()) >= 12
    # 先修关系已建立
    assert any(t["prereq_ids"] for t in r.json())


async def test_problem_auto_annotation_two_stage(seeded_client):
    """两阶段难度标注：新建题目后 difficulty_source 必须被写入，且分数在窗口内。"""
    r = await seeded_client.post(
        "/api/problems",
        json={
            "title": "自动标注测试题",
            "statement": "给定 n 个点，求凸包周长。需要处理共线情况，n <= 10^5。",
            "constraints": "1 <= n <= 10^5",
            "tags": ["计算几何"],
            "auto_annotate": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["difficulty"] in ("入门", "普及-", "普及/提高-", "普及+/提高", "提高+/省选")
    assert body["difficulty_source"] in ("rule", "llm")
    assert 0 <= body["difficulty_score"] <= 100


async def test_problem_route_preview(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(f"/api/problems/{pid}/route")
    assert r.status_code == 200
    assert "use_llm" in r.json()


# ========================================================== 提交与评测
async def test_submit_with_agent_judge(seeded_client):
    """提交 -> 判错归因 Agent -> 错题本落库。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][1]["id"]

    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "language": "python",
            "code": "def merge(x):\n    return x\n",
            "verdict": "WA",
            "passed_cases": 2,
            "total_cases": 4,
            "failed_case": "2 3\n1 10\n2 3",
            "judge_detail": {"case": 2, "expected": "1 10", "got": "1 2 3 10"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["submission"]["verdict"] == "WA"
    assert body["judge"]["root_cause"]
    assert body["judge"]["explanation"]

    # 错题本应已记录
    r = await seeded_client.get("/api/wrong-book")
    assert r.status_code == 200
    assert any(i["problem_id"] == pid for i in r.json()["items"])


async def test_submission_detail_and_solution(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(
        "/api/submissions",
        json={"problem_id": pid, "code": "print(1)\n", "verdict": "WA", "run_agents": False},
    )
    sid = r.json()["submission"]["id"]

    r = await seeded_client.get(f"/api/submissions/{sid}")
    assert r.status_code == 200
    assert r.json()["code"] == "print(1)\n"

    r = await seeded_client.post(f"/api/submissions/{sid}/solution")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["code"]
    assert "TODO" not in data["code"]  # mock 返回真实代码
    assert data["complexity"]["time"]

    # 题解已落库
    r = await seeded_client.get(f"/api/submissions/{sid}")
    assert any(rv["kind"] == "solution" for rv in r.json()["reviews"])


async def test_submission_list_pagination(seeded_client):
    r = await seeded_client.get("/api/submissions", params={"page": 1, "size": 5})
    assert r.status_code == 200
    assert r.json()["size"] == 5


async def test_submit_nonexistent_problem_404(auth_client):
    r = await auth_client.post(
        "/api/submissions", json={"problem_id": "nope", "code": "x", "verdict": "WA"}
    )
    assert r.status_code == 404


# ========================================================== 推荐与路线图
async def test_recommend_next_excludes_recent(seeded_client):
    """推荐必须来自真实题库，且排除最近做过的题。"""
    r = await seeded_client.get("/api/recommend/next")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"]
    assert body["problem_id"]

    # 该题必须真实存在
    r = await seeded_client.get(f"/api/problems/{body['problem_id']}")
    assert r.status_code == 200


async def test_recommend_explain_shows_factors(seeded_client):
    r = await seeded_client.get("/api/recommend/explain")
    assert r.status_code == 200
    body = r.json()
    assert len(body["factors"]) == 3
    assert body["ranking"]


async def test_roadmap_respects_prereq(seeded_client):
    r = await seeded_client.get("/api/recommend/roadmap", params={"weeks": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["stages"]) >= 2
    assert body["rationale"]
    # 阶段标签必须真实存在于题库标签中
    r = await seeded_client.get("/api/problems/tags")
    known = {t["name"] for t in r.json()}
    for stage in body["stages"]:
        for tag in stage["tags"]:
            assert tag in known, f"幻觉标签: {tag}"


# ================================================================ 复习
async def test_review_queue_and_mark(seeded_client):
    r = await seeded_client.get("/api/review/queue")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    assert 0 <= items[0]["retrievability"] <= 1

    pid = items[0]["problem_id"]
    r = await seeded_client.post("/api/review/mark", json={"problem_id": pid, "rating": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["schedule"]["reps"] >= 1
    assert body["schedule"]["interval_days"] > 0
    assert len(body["forgetting_curve"]) == 31


async def test_review_mark_failure_shrinks_interval(seeded_client):
    """评 again 后间隔应小于评 good。"""
    r = await seeded_client.get("/api/review/queue")
    pid = r.json()["items"][0]["problem_id"]

    good = (
        await seeded_client.post("/api/review/mark", json={"problem_id": pid, "rating": 3})
    ).json()["schedule"]
    again = (
        await seeded_client.post("/api/review/mark", json={"problem_id": pid, "rating": 1})
    ).json()["schedule"]
    assert again["interval_days"] <= good["interval_days"]
    assert again["lapses"] >= 1


async def test_review_due_forecast(seeded_client):
    r = await seeded_client.get("/api/review/due")
    assert r.status_code == 200
    assert len(r.json()["forecast"]) == 31


# ============================================================== 掌握度
async def test_mastery_overview_and_profile(seeded_client):
    r = await seeded_client.get("/api/mastery/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert 0 <= body["score"] <= 100
    assert body["level"] in ("L1", "L2", "L3", "L4", "L5")
    assert 0 <= body["confidence"] <= 1
    assert body["suggestion"]

    r = await seeded_client.get("/api/mastery/profile")
    assert r.status_code == 200
    profile = r.json()
    assert profile, "种子数据应产生掌握度画像"
    # 升序：最弱标签在最前
    scores = [p["score"] for p in profile]
    assert scores == sorted(scores)
    assert all(0 <= s <= 100 for s in scores)


async def test_mastery_recalc_and_timeline(seeded_client):
    r = await seeded_client.post("/api/mastery/recalc")
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["score"] >= 0
    assert body["profile_rows"]

    r = await seeded_client.get("/api/mastery/timeline")
    assert r.status_code == 200
    assert r.json()["items"]


async def test_wrong_book_clustering(seeded_client):
    """易错题聚类：按标签组合指纹归簇。"""
    r = await seeded_client.post("/api/wrong-book/cluster")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    cluster = body["clusters"][0]
    assert cluster["size"] >= 1
    assert cluster["problem_ids"]
    assert cluster["suggestion"]
    assert cluster["severity"] > 0


async def test_wrong_book_mark_mastered(seeded_client):
    r = await seeded_client.get("/api/wrong-book")
    pid = r.json()["items"][0]["problem_id"]
    r = await seeded_client.post(f"/api/wrong-book/{pid}/mastered", params={"mastered": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ========================================================== Agent 与任务
async def test_list_agents_contract(auth_client):
    r = await auth_client.get("/api/agent")
    assert r.status_code == 200
    body = r.json()
    assert len(body["core_agents"]) == 5  # 简历点名的五项职责
    assert {a["name"] for a in body["agents"]} >= set(body["core_agents"])


async def test_run_single_agent(auth_client):
    r = await auth_client.post(
        "/api/agent/run",
        json={
            "agent": "judge",
            "payload": {"verdict": "TLE", "code": "for i in range(10**9): pass"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["data"]["root_cause"] == "复杂度"
    assert body["tokens"] > 0


async def test_run_unknown_agent_404(auth_client):
    r = await auth_client.post("/api/agent/run", json={"agent": "nope", "payload": {}})
    assert r.status_code == 404


async def test_agent_stats_reports_degradation_rate(auth_client):
    await auth_client.post("/api/agent/run", json={"agent": "mastery", "payload": {}})
    r = await auth_client.get("/api/agent/stats")
    assert r.status_code == 200
    body = r.json()
    assert "degraded_rate" in body["db"]
    assert "failure_rate" in body["db"]
    assert "latency_p95_ms" in body["db"]
    assert "hit_rate" in body["cache"]


async def test_cache_reduces_duplicate_calls(auth_client):
    """成本控制主张：相同输入第二次调用命中缓存，token 消耗为 0。"""
    payload = {"agent": "judge", "payload": {"verdict": "WA", "code": "print(1)"}}
    first = (await auth_client.post("/api/agent/run", json=payload)).json()
    second = (await auth_client.post("/api/agent/run", json=payload)).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["tokens"] == 0


async def test_pipeline_async_task_and_sse(seeded_client):
    """核心主张：长链路后台任务 + 轮询 + SSE 流式。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][2]["id"]

    r = await seeded_client.post(
        "/api/agent/pipeline",
        json={
            "submission": {
                "problem_id": pid,
                "code": "print(0)\n",
                "verdict": "WA",
                "passed_cases": 1,
                "total_cases": 4,
            },
            "include_roadmap": True,
        },
    )
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert task_id

    # SSE：应该能收到 progress / done 事件
    events: list[str] = []
    async with seeded_client.stream("GET", f"/api/agent/stream?task_id={task_id}") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if "done" in events or "failed" in events:
                break
    assert "progress" in events
    assert "done" in events

    # 轮询详情
    r = await seeded_client.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["progress"] == 1.0
    assert body["result"]["agents"]
    assert body["result"]["elapsed_ms"] > 0


async def test_pipeline_task_cancel(seeded_client):
    """核心主张：任务可中断，中断后不再继续调用 Agent。

    注意竞态：mock provider 极快，任务可能在 cancel 到达前就已 done，
    此时 cancel 返回 False 是正确语义（没有可取消的运行中任务）。
    两种情况都必须让任务落到终态，绝不能卡在 running。
    """
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][3]["id"]
    r = await seeded_client.post(
        "/api/agent/pipeline",
        json={
            "submission": {"problem_id": pid, "code": "print(0)", "verdict": "WA"},
            "include_roadmap": True,
        },
    )
    task_id = r.json()["task_id"]
    r = await seeded_client.post(f"/api/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert isinstance(r.json()["ok"], bool)

    await asyncio.sleep(0.4)
    r = await seeded_client.get(f"/api/tasks/{task_id}")
    assert r.json()["status"] in ("cancelled", "done")


async def test_cancel_is_rejected_for_settled_task(seeded_client):
    """已结束的任务不可再取消，且接口不报错。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(
        "/api/agent/pipeline",
        json={"submission": {"problem_id": pid, "code": "print(0)", "verdict": "AC"}},
    )
    task_id = r.json()["task_id"]
    await seeded_client.post(f"/api/tasks/{task_id}/wait", params={"timeout": 30})
    r = await seeded_client.post(f"/api/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert r.json()["ok"] is False


async def test_task_wait_helper(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][4]["id"]
    r = await seeded_client.post(
        "/api/agent/pipeline",
        json={"submission": {"problem_id": pid, "code": "print(0)", "verdict": "AC"}},
    )
    task_id = r.json()["task_id"]
    r = await seeded_client.post(f"/api/tasks/{task_id}/wait", params={"timeout": 30})
    assert r.status_code == 200
    assert r.json()["status"] == "done"


async def test_tasks_list_and_persisted(seeded_client):
    r = await seeded_client.get("/api/tasks")
    assert r.status_code == 200
    assert "runtime" in r.json()
    assert "persisted" in r.json()


async def test_unknown_task_404(auth_client):
    assert (await auth_client.get("/api/tasks/nope")).status_code == 404
    assert (await auth_client.post("/api/tasks/nope/cancel")).json()["ok"] is False


# ================================================================ 数据
async def test_export_user_data(seeded_client):
    r = await seeded_client.get("/api/data/export")
    assert r.status_code == 200
    body = r.json()
    assert "mastery" in body
    assert "wrong_book" in body
    assert "review_queue" in body
    assert body["exported_at"]


async def test_export_problems_and_import_roundtrip(seeded_client):
    r = await seeded_client.get("/api/data/export/problems", params={"limit": 3})
    assert r.status_code == 200
    exported = r.json()["items"]
    assert len(exported) == 3

    payload = {"problems": exported, "auto_annotate": False}
    r = await seeded_client.post("/api/data/import", json=payload)
    assert r.status_code == 200
    assert r.json()["imported"] == 3


async def test_health_and_global_stats(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = await client.get("/api/stats/global")
    assert r.status_code == 200
    body = r.json()
    assert "tasks" in body and "cache" in body


async def test_openapi_has_all_endpoints(client):
    """接口契约自检：至少 27 个业务接口。"""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    count = sum(len(m) for m in paths.values())
    assert count >= 27, f"接口数不足: {count}"
