"""管理后台端到端验收（管理员分支）。

批次 C 的 smoke 里 admin 分支只在"当前用户恰好是管理员"时才走，
因此这里用种子用户 demo（已提升为管理员）专门验证管理接口全链路。

用法（先启动服务）：
    python scripts/smoke_admin.py --base http://127.0.0.1:8000
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

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)

    def run(self) -> int:
        print("== 管理后台端到端验收 ==\n")

        # 用 demo 登录（种子用户，已提升为管理员）
        r = self.client.post("/api/auth/login", json={"username": "demo", "password": "demo123456"})
        if r.status_code != 200:
            print(f"  [SKIP] demo 登录失败（{r.status_code}），无法验证管理分支")
            return 0

        token = r.json()["access_token"]
        role = r.json().get("role", "user")
        h = {"Authorization": f"Bearer {token}"}
        self.check("demo 登录", True, f"role={role}")

        if role != "admin":
            print("  [SKIP] demo 不是管理员，请先提升角色后再验证")
            return 0

        # ---------- 管理看板 ----------
        r = self.client.get("/api/admin/overview", headers=h)
        ok = r.status_code == 200
        self.check("管理看板可访问", ok, r.text[:80] if not ok else "")
        if ok:
            body = r.json()
            self.check(
                "看板含用户统计",
                "users" in body and "total" in body["users"],
                f"用户 {body['users']['total']} 人",
            )
            self.check(
                "看板含题库统计",
                "problems" in body and "without_testcases" in body["problems"],
                f"缺用例 {body['problems']['without_testcases']} 道",
            )
            self.check(
                "看板含 Agent 统计",
                "agents" in body and "degraded_rate" in body["agents"],
                f"调用 {body['agents']['calls']} 次",
            )
            self.check("看板含任务分布", "tasks" in body, str(body.get("tasks", {}))[:60])

        # ---------- 用户管理 ----------
        r = self.client.get("/api/admin/users", params={"page": 1, "size": 5}, headers=h)
        ok = r.status_code == 200
        self.check("用户列表可读", ok and "items" in r.json())
        target_id = ""
        if ok and r.json()["items"]:
            items = r.json()["items"]
            self.check("用户列表含角色字段", "role" in items[0], f"共 {r.json()['total']} 人")
            # 找一个非管理员的普通用户做改动测试
            normal = next((u for u in items if u["role"] != "admin" and u["id"] != ""), None)
            if normal:
                target_id = normal["id"]

        if target_id:
            # 设为目标 5
            r = self.client.patch(
                f"/api/admin/users/{target_id}", json={"daily_goal": 5}, headers=h
            )
            self.check("修改用户每日目标", r.status_code == 200 and r.json()["daily_goal"] == 5)

            # 停用（会踢下线）
            r = self.client.patch(
                f"/api/admin/users/{target_id}", json={"is_active": False}, headers=h
            )
            self.check(
                "停用用户并踢下线",
                r.status_code == 200 and r.json()["is_active"] is False,
                r.json().get("message", "")[:40],
            )

            # 恢复启用
            r = self.client.patch(
                f"/api/admin/users/{target_id}", json={"is_active": True}, headers=h
            )
            self.check("恢复启用用户", r.status_code == 200 and r.json()["is_active"] is True)

        # ---------- 自我保护 ----------
        me = self.client.get("/api/auth/me", headers=h).json()
        r = self.client.patch(f"/api/admin/users/{me['id']}", json={"role": "user"}, headers=h)
        self.check("不能给自己降级", r.status_code == 400, r.json().get("detail", "")[:40])

        r = self.client.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}, headers=h)
        self.check("不能停用自己", r.status_code == 400)

        # ---------- 题库体检 ----------
        r = self.client.get("/api/admin/problems/health", headers=h)
        ok = r.status_code == 200
        self.check("题库体检可读", ok and "without_testcases" in r.json())
        if ok:
            self.check(
                "体检给出修复提示",
                bool(r.json().get("hint")),
                r.json().get("hint", "")[:50],
            )

        # ---------- 删题（含确认保护） ----------
        r = self.client.post(
            "/api/problems",
            json={
                "title": f"管理验收-待删-{uuid.uuid4().hex[:6]}",
                "statement": "x",
                "tags": ["模拟"],
                "auto_annotate": False,
            },
            headers=h,
        )
        pid = r.json()["id"] if r.status_code == 201 else ""

        if pid:
            r = self.client.delete(f"/api/admin/problems/{pid}", headers=h)
            self.check("未确认时不删除", r.status_code == 400)

            r = self.client.delete(
                f"/api/admin/problems/{pid}", params={"confirm": True}, headers=h
            )
            self.check("确认后删除成功", r.status_code == 200 and r.json()["ok"] is True)

            # 必须带认证头：题目详情接口要求登录，不带头会拿到 401 而非 404，
            # 那样断言"已删除"就失去意义（会误判成失败或误判成成功）
            r = self.client.get(f"/api/problems/{pid}", headers=h)
            self.check(
                "删除后题目不存在",
                r.status_code == 404,
                f"status={r.status_code}",
            )

        # ---------- 数据清理 ----------
        r = self.client.post("/api/admin/cleanup", headers=h)
        ok = r.status_code == 200
        self.check(
            "数据清理可执行",
            ok and "orphan_submissions" in r.json(),
            str(r.json())[:70] if ok else "",
        )

        # ---------- 全局 Agent 统计 ----------
        r = self.client.get("/api/admin/agents", params={"days": 7}, headers=h)
        self.check(
            "全局 Agent 统计",
            r.status_code == 200 and "total_calls" in r.json(),
            f"调用 {r.json().get('total_calls')} 次",
        )

        # ---------- 分页与搜索 ----------
        r = self.client.get("/api/admin/users", params={"keyword": "demo"}, headers=h)
        self.check(
            "用户搜索可用",
            r.status_code == 200 and r.json()["total"] >= 1,
            f"命中 {r.json().get('total')} 人",
        )

        print(f"\n== 结果: {self.passed} 通过, {len(self.failed)} 失败 ==")
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
