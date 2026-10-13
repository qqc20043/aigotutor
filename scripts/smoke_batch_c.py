"""批次 C 端到端验收：认证增强、限流、可观测性、运营功能、管理后台。

用法（先启动服务）：
    python scripts/smoke_batch_c.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import uuid

import httpx


class Smoke:
    def __init__(self, base: str) -> None:
        self.client = httpx.Client(base_url=base.rstrip("/"), timeout=120.0)
        self.passed = 0
        self.failed: list[str] = []
        self.notes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)

    def run(self) -> int:
        print("== 批次 C 端到端验收 ==\n")

        # ---------- 1. 认证增强 ----------
        user = f"batchc{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            "/api/auth/register", json={"username": user, "password": "secret12345"}
        )
        self.check("注册返回双 token", r.status_code == 201 and r.json().get("refresh_token"))
        tokens = r.json()
        access, refresh = tokens["access_token"], tokens["refresh_token"]
        role = tokens.get("role", "user")
        h = {"Authorization": f"Bearer {access}"}

        self.check(
            "token 有效期分离",
            tokens["expires_in"] < tokens["refresh_expires_in"],
            f"access={tokens['expires_in']}s refresh={tokens['refresh_expires_in']}s",
        )

        # refresh 换新 token
        r = self.client.post("/api/auth/refresh", json={"refresh_token": refresh})
        self.check("refresh 换新 token", r.status_code == 200 and r.json().get("access_token"))

        # 类型隔离：access 不能当 refresh 用
        r = self.client.post("/api/auth/refresh", json={"refresh_token": access})
        self.check("access 不能用于 refresh", r.status_code == 401)

        # refresh 不能当 access 用
        r = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
        self.check("refresh 不能访问受保护接口", r.status_code == 401)

        # 改密后旧 token 全失效
        r = self.client.post(
            "/api/auth/change-password",
            json={"old_password": "secret12345", "new_password": "newsecret456"},
            headers=h,
        )
        self.check("改密成功", r.status_code == 200, r.text[:80])
        r = self.client.get("/api/auth/me", headers=h)
        self.check("改密后旧 access 失效", r.status_code == 401)
        r = self.client.post("/api/auth/refresh", json={"refresh_token": refresh})
        self.check("改密后旧 refresh 失效", r.status_code == 401)

        # 用新密码登录
        r = self.client.post("/api/auth/login", json={"username": user, "password": "newsecret456"})
        self.check("新密码可登录", r.status_code == 200)
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 弱密码拒绝
        r = self.client.post(
            "/api/auth/register",
            json={"username": f"weak{uuid.uuid4().hex[:6]}", "password": "12345678"},
        )
        self.check("弱密码被拒（纯数字）", r.status_code == 422)

        # 资料更新
        r = self.client.patch(
            "/api/auth/me",
            json={"nickname": "验收用户", "daily_goal": 2, "timezone": "UTC"},
            headers=h,
        )
        self.check("更新资料（含时区）", r.status_code == 200 and r.json()["timezone"] == "UTC")

        # ---------- 2. 可观测性 ----------
        r = self.client.get("/metrics")
        self.check(
            "/metrics 可抓取",
            r.status_code == 200 and "aigotutor_http_requests_total" in r.text,
        )
        self.check("指标含降级计数", "aigotutor_agent_degraded_total" in r.text)
        self.check("指标含 token 消耗", "aigotutor_llm_tokens_total" in r.text)

        r = self.client.get("/health")
        self.check("响应带 trace id", bool(r.headers.get("X-Trace-Id")))

        # ---------- 3. 题面导入 ----------
        raw = """P9001 验收测试题

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
        r = self.client.post("/api/import/preview", json={"text": raw}, headers=h)
        self.check(
            "题面解析预览",
            r.status_code == 200 and r.json()["preview"]["samples_found"] == 1,
            f"样例={r.json()['preview']['samples_found'] if r.status_code == 200 else '-'}",
        )

        r = self.client.post(
            "/api/import",
            json={
                "text": raw,
                "auto_annotate": True,
                "testcases": [{"input": "3\n1 5 3\n", "output": "5\n", "hidden": False}],
            },
            headers=h,
        )
        self.check(
            "导入题目（含用例）",
            r.status_code == 201 and r.json()["testcases"] == 1,
            f"难度={r.json().get('difficulty') if r.status_code == 201 else '-'}",
        )
        imported_id = r.json().get("id") if r.status_code == 201 else ""

        # 导入的题应可真实判题
        if imported_id:
            judge = self.client.get("/api/judge", headers=h).json()
            if "python" in (judge.get("languages") or []):
                r = self.client.post(
                    "/api/submissions",
                    json={
                        "problem_id": imported_id,
                        "code": "n=int(input())\nprint(max(map(int,input().split())))\n",
                        "language": "python",
                        "judge_mode": "sandbox",
                        "run_agents": False,
                    },
                    headers=h,
                )
                self.check(
                    "导入题可沙箱判题",
                    r.status_code == 201 and r.json()["verdict"] == "AC",
                    f"verdict={r.json().get('verdict') if r.status_code == 201 else r.status_code}",
                )
            else:
                self.notes.append("本机无 python，跳过导入题判题")

        # ---------- 4. 打卡 ----------
        r = self.client.post("/api/checkin", json={"note": "批次C验收"}, headers=h)
        self.check(
            "打卡成功",
            r.status_code == 200 and r.json()["streak"]["current"] >= 1,
            f"连击={r.json().get('streak', {}).get('current') if r.status_code == 200 else '-'}",
        )

        r2 = self.client.post("/api/checkin", json={}, headers=h)
        self.check("重复打卡幂等", r2.status_code == 200 and r2.json()["date"] == r.json()["date"])

        r = self.client.get("/api/checkin/status", headers=h)
        self.check("打卡状态可读", r.status_code == 200 and "history" in r.json())

        # ---------- 5. 排行榜 ----------
        r = self.client.get("/api/leaderboard", params={"metric": "ac", "window": "all"}, headers=h)
        self.check("排行榜可用", r.status_code == 200 and "items" in r.json())
        r = self.client.get("/api/leaderboard", params={"window": "week"}, headers=h)
        self.check("窗口排行带跨时区说明", bool(r.json().get("note")))

        # ---------- 6. 学习报告 ----------
        r = self.client.get("/api/report", params={"days": 7}, headers=h)
        self.check("报告 JSON", r.status_code == 200 and len(r.json()["daily"]) == 7)

        r = self.client.get("/api/report/markdown", params={"days": 7}, headers=h)
        self.check(
            "报告 Markdown",
            r.status_code == 200 and "概览" in r.text and "每日趋势" in r.text,
        )

        # ---------- 7. 通知 ----------
        r = self.client.post("/api/notifications/reminders", headers=h)
        self.check("生成复习提醒", r.status_code == 200)
        r = self.client.get("/api/notifications", headers=h)
        self.check("通知列表", r.status_code == 200 and "unread_count" in r.json())
        r = self.client.post("/api/notifications/read", json={"ids": []}, headers=h)
        self.check("标记已读", r.status_code == 200 and r.json()["ok"] is True)

        # ---------- 8. 管理后台（按角色分支） ----------
        if role == "admin":
            r = self.client.get("/api/admin/overview", headers=h)
            self.check("管理看板", r.status_code == 200 and "users" in r.json())

            r = self.client.get("/api/admin/users", headers=h)
            self.check("用户列表", r.status_code == 200 and "items" in r.json())

            r = self.client.get("/api/admin/problems/health", headers=h)
            self.check("题库体检", r.status_code == 200 and "without_testcases" in r.json())

            r = self.client.delete(f"/api/admin/problems/{imported_id}", headers=h)
            self.check("未确认时不删除", r.status_code == 400)

            r = self.client.post("/api/admin/cleanup", headers=h)
            self.check("数据清理", r.status_code == 200)
        else:
            r = self.client.get("/api/admin/overview", headers=h)
            self.check("非管理员被拒 403", r.status_code == 403)
            self.notes.append("当前用户非管理员，管理接口按 403 分支验证")

        # ---------- 9. 接口总数 ----------
        r = self.client.get("/openapi.json")
        count = sum(len(m) for m in r.json()["paths"].values())
        self.check("接口数量 >= 70", count >= 70, f"实际 {count} 个")

        # ---------- 10. 前端托管 ----------
        r = self.client.get("/")
        self.check("前端 SPA 可访问", r.status_code == 200 and '<div id="app">' in r.text)

        print(f"\n== 结果: {self.passed} 通过, {len(self.failed)} 失败 ==")
        for n in self.notes:
            print(f"  备注: {n}")
        if self.failed:
            print("失败项: " + ", ".join(self.failed))
            return 1
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    sys.exit(Smoke(args.base).run())


if __name__ == "__main__":
    main()
