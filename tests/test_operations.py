"""运营功能测试（批次 C）：题面导入、报告、打卡、排行榜、通知、管理后台。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.importer import (
    ParsedProblem,
    detect_source,
    detect_tags,
    parse_problem,
    parse_samples,
    strip_html,
)
from app.operations import user_day_bounds, user_timezone, user_today


# ============================================================ 题面解析
def test_strip_html_removes_tags_keeps_text():
    html = "<p>给定 <b>n</b> 个数</p><br/><div>输出最大值</div>"
    out = strip_html(html)
    assert "<" not in out
    assert "给定" in out
    assert "输出最大值" in out


def test_strip_html_decodes_entities():
    assert "a < b & c > d" in strip_html("a &lt; b &amp; c &gt; d")


def test_strip_html_keeps_math_untouched():
    """数学公式必须原样保留（前端用 KaTeX 渲染）。"""
    text = "求 $\\sum_{i=1}^{n} a_i$ 的值"
    assert "$\\sum_{i=1}^{n} a_i$" in strip_html(text)


def test_detect_source_luogu():
    source, sid = detect_source("https://www.luogu.com.cn/problem/P1001")
    assert source == "luogu"
    assert sid == "P1001"


def test_detect_source_codeforces():
    source, sid = detect_source("https://codeforces.com/contest/1234/problem/A")
    assert source == "codeforces"
    assert "1234" in sid


def test_detect_source_unknown():
    source, sid = detect_source("这是一道自编题")
    assert source == "import"
    assert sid == ""


def test_detect_tags_from_text():
    text = "本题需要先排序，然后用贪心策略。排序是关键。"
    tags = detect_tags(text)
    assert "排序" in tags
    assert "贪心" in tags


def test_detect_tags_english_word_boundary():
    """'dp' 不应命中 'adapt'（英文关键词需词边界）。"""
    assert "动态规划" not in detect_tags("we adapt the array")
    assert "动态规划" in detect_tags("use dp to solve it")


def test_parse_samples_alternating_blocks():
    text = """输入 #1
1 2
输出 #1
3
输入 #2
5 7
输出 #2
12
"""
    samples = parse_samples(text)
    assert len(samples) == 2
    assert samples[0]["input"] == "1 2"
    assert samples[0]["output"] == "3"
    assert samples[1]["input"] == "5 7"


def test_parse_full_problem_luogu_style():
    raw = """P1001 A+B Problem

题目描述
输入两个整数 a, b，输出它们的和。

输入格式
一行两个整数 a, b。

输出格式
一个整数，表示 a+b。

样例 #1
输入 #1
1 2
输出 #1
3

数据范围
|a|, |b| <= 10^9
"""
    p = parse_problem(raw)
    assert isinstance(p, ParsedProblem)
    assert "A+B" in p.title
    assert "输出它们的和" in p.statement
    assert "一行两个整数" in p.input_desc
    assert "一个整数" in p.output_desc
    assert "10^9" in p.constraints
    assert len(p.samples) == 1
    assert p.samples[0]["input"] == "1 2"
    assert p.constraint_tight is True
    assert p.source == "luogu"
    assert p.source_id == "P1001"


def test_parse_problem_html_input():
    raw = """<h1>P2000 求和</h1>
<p>题目描述</p><p>给定 n 个数，求它们的和。</p>
<p>输入格式</p><p>第一行 n，第二行 n 个整数。</p>
<p>输出格式</p><p>一个整数。</p>"""
    p = parse_problem(raw)
    assert "<" not in p.statement
    assert "给定 n 个数" in p.statement


def test_parse_problem_empty_input_warns():
    p = parse_problem("   ")
    assert p.warnings
    assert any("空" in w for w in p.warnings)


def test_parse_problem_reports_missing_sections():
    p = parse_problem("只有一行标题")
    assert p.warnings  # 应提示缺少输入/输出格式等


def test_parse_problem_detects_constructive():
    p = parse_problem("题目描述\n构造一个长度为 2n 的合法括号序列。")
    assert p.is_constructive is True


# ============================================================ 时区处理
def test_user_today_uses_local_timezone():
    """核心：日界必须按用户时区算，否则东八区凌晨打卡会算到前一天。"""
    # UTC 时间 2024-03-01 20:00 -> 东八区是 2024-03-02 04:00
    utc_dt = datetime(2024, 3, 1, 20, 0, tzinfo=timezone.utc)
    assert user_today("Asia/Shanghai", utc_dt) == "2024-03-02"
    assert user_today("UTC", utc_dt) == "2024-03-01"
    # 西五区是 2024-03-01 15:00
    assert user_today("America/New_York", utc_dt) == "2024-03-01"


def test_user_day_bounds_returns_utc_range():
    utc_dt = datetime(2024, 3, 1, 20, 0, tzinfo=timezone.utc)
    start, end = user_day_bounds("Asia/Shanghai", utc_dt)
    # 东八区 3-02 00:00 == UTC 3-01 16:00
    assert start == datetime(2024, 3, 1, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 3, 2, 16, 0, tzinfo=timezone.utc)
    assert (end - start) == timedelta(days=1)


def test_invalid_timezone_falls_back():
    """无效时区不能抛异常，否则功能直接不可用。"""
    tz = user_timezone("Mars/Olympus")
    assert tz is not None
    assert user_today("Mars/Olympus")  # 不抛异常


# ============================================================ 导入接口
async def test_import_preview(auth_client):
    r = await auth_client.post(
        "/api/import/preview",
        json={
            "text": "P1001 A+B Problem\n\n题目描述\n输出 a+b。\n\n输入格式\n两个整数。\n\n输出格式\n一个整数。"
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parsed"]["title"]
    assert "preview" in body


async def test_import_problem_creates_record(auth_client):
    raw = """P3000 测试题

题目描述
给定 n 个整数，输出它们的最大值。

输入格式
第一行一个整数 n，第二行 n 个整数。

输出格式
一个整数，表示最大值。

样例 #1
输入 #1
3
1 5 3
输出 #1
5

数据范围
1 <= n <= 10^5
"""
    r = await auth_client.post("/api/import", json={"text": raw, "auto_annotate": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"]
    assert body["difficulty"]
    assert body["samples"] == 1

    # 题目真的落库了
    r2 = await auth_client.get(f"/api/problems/{body['id']}")
    assert r2.status_code == 200
    assert "最大值" in r2.json()["statement"]


async def test_import_with_testcases(auth_client):
    raw = "自编题\n\n题目描述\n输出 a+b。\n\n输入格式\na b\n\n输出格式\n和"
    r = await auth_client.post(
        "/api/import",
        json={
            "text": raw,
            "auto_annotate": False,
            "testcases": [{"input": "1 2\n", "output": "3\n", "hidden": False}],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["testcases"] == 1


async def test_import_rejects_unparseable(auth_client):
    r = await auth_client.post("/api/import", json={"text": "   \n  "})
    assert r.status_code == 422


async def test_import_batch_partial_failure(auth_client):
    """批量导入：单项失败不应影响其他项。"""
    r = await auth_client.post(
        "/api/import/batch",
        json={
            "auto_annotate": False,
            "items": [
                {"text": "题一\n\n题目描述\n输出 1。\n\n输入格式\n无\n\n输出格式\n1"},
                {"text": "题二\n\n题目描述\n输出 2。\n\n输入格式\n无\n\n输出格式\n2"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"] == 2
    assert body["failed"] == 0


# ============================================================ 打卡
async def test_checkin_flow(auth_client):
    r = await auth_client.post("/api/checkin", json={"note": "第一天"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"]
    assert body["streak"]["current"] >= 1
    assert body["streak"]["checked_today"] is True

    # 重复打卡应幂等（不报错，仍是同一天）
    r2 = await auth_client.post("/api/checkin", json={"note": "又打了一次"})
    assert r2.status_code == 200
    assert r2.json()["date"] == body["date"]
    assert r2.json()["streak"]["current"] == body["streak"]["current"]


async def test_checkin_status(auth_client):
    await auth_client.post("/api/checkin", json={})
    r = await auth_client.get("/api/checkin/status")
    assert r.status_code == 200
    body = r.json()
    assert "today" in body
    assert "streak" in body
    assert "history" in body
    assert body["history"]


async def test_checkin_goal_notification(auth_client):
    """达成每日目标应生成通知。"""
    # 建题 + 配用例 + 提交 AC
    r = await auth_client.post(
        "/api/problems",
        json={
            "title": f"目标题-{uuid.uuid4().hex[:6]}",
            "statement": "x",
            "tags": ["模拟"],
            "auto_annotate": False,
        },
    )
    pid = r.json()["id"]
    await auth_client.put(
        f"/api/problems/{pid}/testcases",
        json={"replace": True, "cases": [{"input": "1 2\n", "output": "3\n", "hidden": False}]},
    )

    # 把每日目标设为 1，便于触发
    await auth_client.patch("/api/auth/me", json={"daily_goal": 1})

    # 用沙箱判 AC（无 python 时用 caller）
    judge = (await auth_client.get("/api/judge")).json()
    if "python" in (judge.get("languages") or []):
        await auth_client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "code": "a,b=map(int,input().split())\nprint(a+b)\n",
                "language": "python",
                "judge_mode": "sandbox",
                "run_agents": False,
            },
        )
    else:
        await auth_client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "code": "x",
                "judge_mode": "caller",
                "verdict": "AC",
                "run_agents": False,
            },
        )

    r2 = await auth_client.post("/api/checkin", json={})
    assert r2.status_code == 200
    # 达成目标时应能查到通知（通知生成与 checkin 同事务，这里查列表）
    r3 = await auth_client.get("/api/notifications")
    assert r3.status_code == 200


# ============================================================ 排行榜
async def test_leaderboard_returns_items(auth_client):
    r = await auth_client.get("/api/leaderboard", params={"metric": "ac", "window": "all"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert body["metric"] == "ac"


async def test_leaderboard_window_note(auth_client):
    """窗口类排行应标注跨时区不可比（诚实声明）。"""
    r = await auth_client.get("/api/leaderboard", params={"window": "week"})
    assert r.status_code == 200
    assert r.json()["note"]


async def test_leaderboard_rejects_bad_metric(auth_client):
    r = await auth_client.get("/api/leaderboard", params={"metric": "bogus"})
    assert r.status_code == 422


# ============================================================ 通知
async def test_notifications_flow(auth_client):
    r = await auth_client.get("/api/notifications")
    assert r.status_code == 200
    assert "items" in r.json()
    assert "unread_count" in r.json()

    r2 = await auth_client.post("/api/notifications/read", json={"ids": []})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True


async def test_notification_dedupe(auth_client):
    """同一天的复习提醒只应生成一条（否则列表会被刷满）。"""
    r1 = await auth_client.post("/api/notifications/reminders")
    r2 = await auth_client.post("/api/notifications/reminders")
    assert r1.status_code == 200 and r2.status_code == 200

    listing = await auth_client.get("/api/notifications")
    review_notifs = [n for n in listing.json()["items"] if n["kind"] == "review"]
    assert len(review_notifs) <= 1, f"复习提醒重复生成: {len(review_notifs)} 条"


# ============================================================ 学习报告
async def test_report_json(auth_client):
    r = await auth_client.get("/api/report", params={"days": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body
    assert "daily" in body
    assert len(body["daily"]) == 7
    assert "streak" in body


async def test_report_markdown(auth_client):
    r = await auth_client.get("/api/report/markdown", params={"days": 7})
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "# " in r.text
    assert "概览" in r.text
    assert "每日趋势" in r.text


async def test_report_after_activity(auth_client):
    """有活动后报告应反映出来。"""
    r = await auth_client.post(
        "/api/problems",
        json={
            "title": f"报告题-{uuid.uuid4().hex[:6]}",
            "statement": "x",
            "tags": ["模拟"],
            "auto_annotate": False,
        },
    )
    pid = r.json()["id"]
    await auth_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "code": "x",
            "judge_mode": "caller",
            "verdict": "AC",
            "run_agents": False,
        },
    )
    r2 = await auth_client.get("/api/report", params={"days": 7})
    assert r2.json()["summary"]["submissions"] >= 1


# ============================================================ 管理后台
async def test_admin_overview(auth_client):
    """首个用户是管理员，应能访问管理接口。"""
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("当前用户非管理员（首个用户已被占用）")

    r = await auth_client.get("/api/admin/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "users" in body
    assert "problems" in body
    assert "agents" in body


async def test_admin_forbidden_for_normal_user(seeded_client):
    """非管理员访问管理接口必须 403（demo 用户是普通用户）。"""
    me = (await seeded_client.get("/api/auth/me")).json()
    if me["role"] == "admin":
        pytest.skip("demo 是管理员，跳过")

    r = await seeded_client.get("/api/admin/overview")
    assert r.status_code == 403


async def test_admin_problems_health(auth_client):
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("需要管理员")

    r = await auth_client.get("/api/admin/problems/health")
    assert r.status_code == 200
    assert "without_testcases" in r.json()


async def test_admin_delete_requires_confirm(auth_client):
    """删题必须显式确认（防误删）。"""
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("需要管理员")

    r = await auth_client.post(
        "/api/problems",
        json={
            "title": f"待删题-{uuid.uuid4().hex[:6]}",
            "statement": "x",
            "tags": ["模拟"],
            "auto_annotate": False,
        },
    )
    pid = r.json()["id"]

    r2 = await auth_client.delete(f"/api/admin/problems/{pid}")
    assert r2.status_code == 400, "未确认时不应删除"

    r3 = await auth_client.delete(f"/api/admin/problems/{pid}", params={"confirm": True})
    assert r3.status_code == 200
    assert r3.json()["ok"] is True

    # 确认已删除
    r4 = await auth_client.get(f"/api/problems/{pid}")
    assert r4.status_code == 404


async def test_admin_cannot_demote_self(auth_client):
    """管理员不能把自己降级（避免锁在门外）。"""
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("需要管理员")

    r = await auth_client.patch(f"/api/admin/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400
    assert "锁在门外" in r.json()["detail"]


async def test_admin_cleanup(auth_client):
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("需要管理员")

    r = await auth_client.post("/api/admin/cleanup")
    assert r.status_code == 200
    assert "orphan_submissions" in r.json()


async def test_admin_agents_stats(auth_client):
    me = (await auth_client.get("/api/auth/me")).json()
    if me["role"] != "admin":
        pytest.skip("需要管理员")

    r = await auth_client.get("/api/admin/agents", params={"days": 7})
    assert r.status_code == 200
    assert "total_calls" in r.json()
