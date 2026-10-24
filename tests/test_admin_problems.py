"""管理后台题目管理的回归测试：一站式建题、治理改题、权限边界。

对标 Hydro 的内容工作台：管理员建题（题面+用例一次提交）→ 题立即可判题，
学生做题 → 真实沙箱 AC —— "出题到验收"完整闭环。
"""

from __future__ import annotations

import uuid


async def _admin_and_student(client):
    """返回 (admin_token, student_token)。admin 由首个注册 + 提权构造。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User as UserModel

    a_name = f"adm{uuid.uuid4().hex[:8]}"
    s_name = f"stu{uuid.uuid4().hex[:8]}"
    ra = await client.post(
        "/api/auth/register", json={"username": a_name, "password": "test123456", "nickname": "A"}
    )
    rs = await client.post(
        "/api/auth/register", json={"username": s_name, "password": "test123456", "nickname": "S"}
    )
    ta, ts = ra.json()["access_token"], rs.json()["access_token"]
    async with session_scope() as db:
        row = (await db.execute(select(UserModel).where(UserModel.username == a_name))).scalar_one()
        row.role = "admin"
    return ta, ts


async def test_admin_create_problem_one_stop(client):
    """管理员一站式建题：题面+标签+用例，返回可判题状态，用例数正确。"""
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}

    r = await client.post(
        "/api/admin/problems",
        headers=H,
        json={
            "title": "管理台建题-两数之和",
            "statement": "输入两个整数 a b，输出它们的和。",
            "input_desc": "一行两个整数",
            "output_desc": "一行一个整数",
            "tags": ["模拟", "入门"],
            "difficulty": "入门",
            "testcases": [
                {"input": "1 2\n", "output": "3\n", "hidden": "false"},
                {"input": "10 20\n", "output": "30\n", "hidden": "true"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["cases_saved"] == 2
    assert body["judgeable"] is True
    pid = body["id"]

    # 列表可见且健康（case_count=2）
    lst = (await client.get("/api/admin/problems?page=1&size=10", headers=H)).json()
    item = next(i for i in lst["items"] if i["id"] == pid)
    assert item["case_count"] == 2 and item["created_by"], "归属管理员"

    # 学生视角：题库可见 → 提交 AC（真实沙箱闭环）
    r = await client.post(
        "/api/submissions",
        headers={"Authorization": f"Bearer {ts}"},
        json={
            "problem_id": pid,
            "language": "python",
            "code": "a, b = map(int, input().split())\nprint(a + b)\n",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["verdict"] == "AC", "一站式建的题必须能真实判题"


async def test_admin_patch_problem(client):
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    pid = (
        (
            await client.post(
                "/api/admin/problems",
                headers=H,
                json={"title": "改题前", "statement": "旧题面", "tags": ["模拟"]},
            )
        ).json()
    )["id"]

    r = await client.patch(
        f"/api/admin/problems/{pid}",
        headers=H,
        json={"title": "改题后", "time_limit_ms": 2000},
    )
    assert r.status_code == 200
    assert set(r.json()["updated"]) == {"title", "time_limit_ms"}

    # 学生读到的就是新题面
    detail = (
        await client.get(f"/api/problems/{pid}", headers={"Authorization": f"Bearer {ts}"})
    ).json()
    assert detail["title"] == "改题后"


async def test_admin_problems_requires_admin(client):
    _ta, ts = await _admin_and_student(client)
    r = await client.get("/api/admin/problems", headers={"Authorization": f"Bearer {ts}"})
    assert r.status_code == 403
    r = await client.post(
        "/api/admin/problems",
        headers={"Authorization": f"Bearer {ts}"},
        json={"title": "x", "statement": "y"},
    )
    assert r.status_code == 403
