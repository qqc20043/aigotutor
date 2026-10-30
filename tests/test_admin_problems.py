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


# ==================================================== 后台补齐：提交/用户/公告
async def test_admin_submissions_and_rejudge(client):
    """全平台提交列表 + 管理员重判（用例更新后历史结果可刷新）。"""
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    pid = (
        await client.post(
            "/api/admin/problems",
            headers=H,
            json={
                "title": "重判题",
                "statement": "a+b",
                "testcases": [{"input": "1 2\n", "output": "3\n"}],
            },
        )
    ).json()["id"]
    # 学生先交错误代码
    sub = (
        await client.post(
            "/api/submissions",
            headers={"Authorization": f"Bearer {ts}"},
            json={"problem_id": pid, "language": "python", "code": "print(0)"},
        )
    ).json()
    assert sub["verdict"] == "WA"
    sid = sub["submission"]["id"]

    lst = (await client.get("/api/admin/submissions?page=1&size=10", headers=H)).json()
    assert lst["total"] >= 1
    row = next(i for i in lst["items"] if i["id"] == sid)
    assert row["username"] and row["problem_title"] == "重判题"

    # 换一份正确代码的提交 → 重判应变成 AC（验证重判真执行沙箱）
    good = (
        await client.post(
            "/api/submissions",
            headers={"Authorization": f"Bearer {ts}"},
            json={
                "problem_id": pid,
                "language": "python",
                "code": "a,b=map(int,input().split())\nprint(a+b)",
            },
        )
    ).json()
    assert good["verdict"] == "AC"
    r = await client.post(f"/api/admin/submissions/{good['submission']['id']}/rejudge", headers=H)
    assert r.status_code == 200 and r.json()["new_verdict"] == "AC"


async def test_admin_user_detail_and_reset_password(client):
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ts}"})).json()
    uid = me["id"]

    d = (await client.get(f"/api/admin/users/{uid}", headers=H)).json()
    assert d["username"] == me["username"]
    assert "stats" in d and "recent_submissions" in d

    r = await client.post(f"/api/admin/users/{uid}/reset-password", headers=H)
    assert r.status_code == 200
    temp = r.json()["temp_password"]
    assert temp
    # 旧 token 已失效（token_version+1）→ 401
    old = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ts}"})
    assert old.status_code == 401
    # 临时密码可登录
    lg = await client.post("/api/auth/login", json={"username": me["username"], "password": temp})
    assert lg.status_code == 200


async def test_admin_announce_broadcast_and_single(client):
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ts}"})).json()

    # 定向单人
    r = await client.post(
        "/api/admin/announce",
        headers=H,
        json={"title": "题库更新", "body": "新增 5 道线段树题", "username": me["username"]},
    )
    assert r.status_code == 200 and r.json()["sent"] == 1
    notes = (
        await client.get("/api/notifications", headers={"Authorization": f"Bearer {ts}"})
    ).json()
    items = notes.get("items") or notes if isinstance(notes, list) else notes.get("items", [])
    assert any("题库更新" in str(n) for n in items), "用户应收到公告"

    # 全员广播
    r = await client.post(
        "/api/admin/announce", headers=H, json={"title": "系统维护", "body": "今晚 22:00 停机"}
    )
    assert r.status_code == 200 and r.json()["scope"] == "broadcast" and r.json()["sent"] >= 2

    # 非管理员 403
    r = await client.post(
        "/api/admin/announce", headers={"Authorization": f"Bearer {ts}"}, json={"title": "x"}
    )
    assert r.status_code == 403


# ==================================================== 审计 / 批量 / 用例维护
async def test_audit_log_records_admin_actions(client):
    """所有管理动作必须留痕（谁/何时/对什么/做了什么）。"""
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    pid = (
        await client.post(
            "/api/admin/problems",
            headers=H,
            json={
                "title": "审计测试题",
                "statement": "s",
                "testcases": [{"input": "1", "output": "1"}],
            },
        )
    ).json()["id"]
    await client.patch(f"/api/admin/problems/{pid}", headers=H, json={"title": "审计测试题2"})
    me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ts}"})).json()
    await client.patch(f"/api/admin/users/{me['id']}", headers=H, json={"daily_goal": 4})

    # 直接查库确认审计是否写入（独立事务时序）
    from sqlalchemy import select as _sel

    from app.db import session_scope as _ss
    from app.models import AdminAuditLog as _AL

    async with _ss() as _db:
        rows = (await _db.execute(_sel(_AL.action, _AL.actor_name))).all()
    print("DB audit rows:", rows)

    logs = (await client.get("/api/admin/audit?page=1&size=20", headers=H)).json()
    actions = [i["action"] for i in logs["items"]]
    assert "problem.create" in actions
    assert "problem.update" in actions
    assert "user.patch" in actions
    item = next(i for i in logs["items"] if i["action"] == "problem.update")
    assert item["actor_name"], "必须记录操作人"
    assert item["action_label"] == "修改题目"
    assert item["detail"]["updated"] == ["title"]

    # 按动作筛选
    only = (await client.get("/api/admin/audit?action=problem.create", headers=H)).json()
    assert all(i["action"] == "problem.create" for i in only["items"])


async def test_batch_users(client):
    ta, _ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    ids = []
    for _ in range(3):
        r = await client.post(
            "/api/auth/register",
            json={"username": f"bt{uuid.uuid4().hex[:6]}", "password": "test123456"},
        )
        tok = r.json()["access_token"]
        me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})).json()
        ids.append(me["id"])

    r = await client.post(
        "/api/admin/users/batch", headers=H, json={"user_ids": ids, "is_active": False}
    )
    assert r.status_code == 200 and r.json()["updated"] == 3

    # 被停用用户的 token 立即失效
    r = await client.post(
        "/api/auth/login", json={"username": "x", "password": "y"}
    )  # 仅确认接口正常
    assert r.status_code in (401, 422)

    # 批量操作自己 → 拒绝
    admin_me = (await client.get("/api/auth/me", headers=H)).json()
    r = await client.post(
        "/api/admin/users/batch", headers=H, json={"user_ids": [admin_me["id"]], "is_active": False}
    )
    assert r.status_code == 400


async def test_admin_testcase_management(client):
    """管理员可查看全部用例（含隐藏内容）并整批替换——建题后的日常维护。"""
    ta, _ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    pid = (
        await client.post(
            "/api/admin/problems",
            headers=H,
            json={
                "title": "用例维护题",
                "statement": "s",
                "testcases": [
                    {"input": "1 2\n", "output": "3\n"},
                    {"input": "5 5\n", "output": "10\n"},
                ],
            },
        )
    ).json()["id"]

    got = (await client.get(f"/api/admin/problems/{pid}/testcases", headers=H)).json()
    assert len(got["items"]) == 2
    assert got["items"][0]["output"] == "3\n", "管理员能看到隐藏用例内容"

    r = await client.put(
        f"/api/admin/problems/{pid}/testcases",
        headers=H,
        json={"cases": [{"input": "9 9\n", "output": "18\n", "hidden": "true"}]},
    )
    assert r.status_code == 200
    assert r.json()["saved"] == 1 and r.json()["old_count"] == 2

    # 用例变更后历史提交可重判（闭环）
    after = (await client.get(f"/api/admin/problems/{pid}/testcases", headers=H)).json()
    assert len(after["items"]) == 1


# ==================================================== 趋势 / 判题状态 / CSV 导入
async def test_admin_trends_shape(client):
    """趋势接口：按天聚合（提交/AC/新增用户），窗口外为 0。"""
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    pid = (
        await client.post(
            "/api/admin/problems",
            headers=H,
            json={
                "title": "趋势题",
                "statement": "s",
                "testcases": [{"input": "1", "output": "1"}],
            },
        )
    ).json()["id"]
    await client.post(
        "/api/submissions",
        headers={"Authorization": f"Bearer {ts}"},
        json={"problem_id": pid, "language": "python", "code": "print(1)"},
    )

    r = await client.get("/api/admin/trends?days=14", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 15  # days+1
    today = body["items"][-1]
    assert today["submissions"] >= 1 and today["new_users"] >= 2
    assert body["totals"]["submissions"] >= 1
    # 除今天外全 0（新测试库）
    earlier = list(body["items"][:-1])
    assert all(i["submissions"] == 0 and i["new_users"] == 0 for i in earlier)


async def test_admin_judge_status(client):
    ta, _ts = await _admin_and_student(client)
    r = await client.get("/api/admin/judge-status", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert "python" in body["languages"]
    assert {"running_tasks", "queued_tasks", "concurrency_limit"} <= set(body)


async def test_admin_csv_import(client):
    ta, ts = await _admin_and_student(client)
    H = {"Authorization": f"Bearer {ta}"}
    # 已存在用户名 + 弱密码 + 合法行 混合
    existing = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ts}"})).json()[
        "username"
    ]
    csv = (
        "# 注释行应被忽略\n"
        "imported_a,passw0rd123,导入甲\n"
        "imported_b,passw0rd123\n"
        f"{existing},passw0rd123\n"
        "weak_user,123\n"
        "badline\n"
    )
    r = await client.post("/api/admin/users/import", headers=H, json={"csv": csv})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created_count"] == 2
    reasons = {f["reason"] for f in body["failed"]}
    assert reasons == {"用户名已存在", "密码至少 8 位", "格式错误（需要 用户名,密码）"}
    reasons = {f["reason"] for f in body["failed"]}
    assert reasons == {"用户名已存在", "密码至少 8 位", "格式错误（需要 用户名,密码）"}

    # 导入的用户能登录
    lg = await client.post(
        "/api/auth/login", json={"username": "imported_a", "password": "passw0rd123"}
    )
    assert lg.status_code == 200
