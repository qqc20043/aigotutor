"""MCP Server 的工具层测试：身份解析、11 个工具的 mock 路径与治理契约。

设计文档：docs/PRD-2026-10-24-MCP服务与模拟面试.md Feature A。
工具实现全部走 service/pipeline 层，因此这里断言的是"MCP 通道的行为与
Web 口径一致"（尤其提示强度治理），而不是重复业务测试。
"""

from __future__ import annotations

import pytest

import app.mcp_server as mcp_mod
from app.db import session_scope
from app.services import create_problem


@pytest.fixture(autouse=True)
async def _mcp_identity():
    """把 MCP 身份固定为当场注册的用户（隔离且可复现）。"""
    import uuid

    from app.services import create_user

    username = f"mcp{uuid.uuid4().hex[:8]}"
    async with session_scope() as db:
        await create_user(db, username=username, password_hash="x", nickname=username)
    mcp_mod._MCP_USER_ID = None
    import os

    old = os.environ.get("AIGOTUTOR_MCP_USER")
    os.environ["AIGOTUTOR_MCP_USER"] = username
    yield username
    if old is None:
        os.environ.pop("AIGOTUTOR_MCP_USER", None)
    else:
        os.environ["AIGOTUTOR_MCP_USER"] = old
    mcp_mod._MCP_USER_ID = None


async def _seed_problem(title="两数之和", tags=("模拟",), with_cases=False) -> str:
    async with session_scope() as db:
        p = await create_problem(
            db, {"title": title, "statement": f"{title}的题面"}, tag_names=list(tags)
        )
        pid = p.id
    return pid


async def test_resolve_user_and_missing_user():
    uid = await mcp_mod._resolve_user()
    assert uid

    import os

    mcp_mod._MCP_USER_ID = None
    os.environ["AIGOTUTOR_MCP_USER"] = "no_such_user_xyz"
    try:
        with pytest.raises(SystemExit):
            await mcp_mod._resolve_user()
    finally:
        os.environ["AIGOTUTOR_MCP_USER"] = "demo"
        mcp_mod._MCP_USER_ID = None


async def test_tool_get_problem_and_search():
    pid = await _seed_problem("MCP检索题")
    out = await mcp_mod._tool_get_problem(pid)
    assert out["problem_id"] == pid
    assert out["title"] == "MCP检索题"
    assert "samples" in out and "statement" in out

    err = await mcp_mod._tool_get_problem("not-exist")
    assert "error" in err

    res = await mcp_mod._tool_search("MCP检索", None, 5)
    assert any(i["problem_id"] == pid for i in res["items"])


async def test_tool_hint_strength_governance():
    """MCP 通道的提示强度必须与 Web 同口径：0 次提交给最强档。"""
    pid = await _seed_problem()
    out = await mcp_mod._tool_get_hint(pid, attempts=0)
    assert out["strength"] == "pseudocode", "首次接触（0 次提交）应给最强档"
    assert "strength_reason" in out
    assert out["hints"] and "note" in out, "必须注明强度由系统算定"

    out2 = await mcp_mod._tool_get_hint(pid, attempts=10)  # 新用户无画像 → mastery 50
    assert out2["strength"] in ("direction", "key_observation", "pseudocode")


async def test_tool_similar():
    a = await _seed_problem("区间合并A", ("贪心", "排序"))
    b = await _seed_problem("会议室安排B", ("贪心",))
    out = await mcp_mod._tool_similar(a, 5)
    ids = [i["problem_id"] for i in out["items"]]
    assert b in ids, "同标签题必须被召回"
    assert a not in ids, "不得返回自身"


async def test_tool_mastery_empty_and_next_problem():
    """新用户：画像走样本不足分支；推荐可运行并返回候选之一。"""
    m = await mcp_mod._tool_mastery()
    assert "score" in m or "note" in m

    await _seed_problem("可推荐题C")
    rec = await mcp_mod._tool_next_problem(None)
    assert "recommendation" in rec and "reason" in rec


async def test_tool_review_and_mark():
    uid = await mcp_mod._resolve_user()
    pid = await _seed_problem("复习打分题")
    async with session_scope() as db:
        from app.services import upsert_review_item

        await upsert_review_item(db, uid, pid, 3)
    q = await mcp_mod._tool_review_queue(30)
    assert "items" in q

    out = await mcp_mod._tool_mark_review(pid, 4)
    assert out["ok"] is True and "next_interval_days" in out
    bad = await mcp_mod._tool_mark_review(pid, 9)
    assert "error" in bad


async def test_tool_wrong_book_and_roadmap():
    wb = await mcp_mod._tool_wrong_book(5)
    assert "items" in wb and "error_clusters" in wb
    rm = await mcp_mod._tool_roadmap(6)
    assert "stages" in rm or "rationale" in rm


async def test_tool_submit_solution_real_judge():
    """MCP 提交走真实沙箱：先配用例，AC 代码应判 AC 并落库。"""
    from app.judge_service import replace_testcases
    from app.services import get_problem

    pid = await _seed_problem("MCP判题题")
    async with session_scope() as db:
        p = await get_problem(db, pid)
        await replace_testcases(
            db,
            pid,
            [
                {"input": "1 2\n", "output": "3\n", "hidden": False},
                {"input": "4 5\n", "output": "9\n", "hidden": True},
            ],
        )
        assert p is not None
    out = await mcp_mod._tool_submit(pid, "python", "a,b=map(int,input().split());print(a+b)")
    assert out["verdict"] == "AC" and out["passed"] == 2
    assert out["submission_id"]

    bad = await mcp_mod._tool_submit(pid, "python", "print(0)")
    assert bad["verdict"] == "WA"
