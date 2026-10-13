"""批次 B 前端验收：验证构建产物完整、静态托管可用、API 契约与前端调用一致。

用法（先启动服务）：
    python scripts/smoke_web.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


class Smoke:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=120.0)
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
        print("== 批次 B 前端验收 ==\n")

        # ---------- 1. 构建产物完整性 ----------
        dist = ROOT / "web" / "dist"
        self.check("dist 目录存在", dist.is_dir(), str(dist))
        index = dist / "index.html"
        self.check("index.html 存在", index.exists())

        assets = list((dist / "assets").glob("*.js")) if (dist / "assets").is_dir() else []
        self.check("JS 产物已生成", len(assets) >= 5, f"{len(assets)} 个 js 文件")

        css = list((dist / "assets").glob("*.css")) if (dist / "assets").is_dir() else []
        self.check("CSS 产物已生成", len(css) >= 2, f"{len(css)} 个 css 文件")

        # 关键页面 chunk 必须存在（说明按路由拆包生效）
        names = " ".join(a.name for a in assets)
        for page in ("LoginView", "ProblemDetailView", "AgentView", "MasteryView", "ReviewView"):
            self.check(f"路由分包：{page}", page in names)

        total_kb = sum(a.stat().st_size for a in assets) // 1024
        self.notes.append(f"JS 产物总大小 {total_kb} KB（{len(assets)} 个文件）")

        # ---------- 2. 静态托管 ----------
        r = self.client.get("/")
        self.check("根路径返回 SPA", r.status_code == 200 and '<div id="app">' in r.text)
        html = r.text

        m = re.search(r'src="(/assets/[^"]+\.js)"', html)
        self.check("index.html 引用打包 JS", m is not None)
        if m:
            ra = self.client.get(m.group(1))
            self.check(
                "打包 JS 可访问",
                ra.status_code == 200 and len(ra.content) > 1000,
                f"{len(ra.content)} bytes",
            )

        m2 = re.search(r'href="(/assets/[^"]+\.css)"', html)
        if m2:
            rc = self.client.get(m2.group(1))
            self.check("打包 CSS 可访问", rc.status_code == 200, f"{len(rc.content)} bytes")

        # ---------- 3. API 契约（前端实际调用的接口） ----------
        user = f"web{uuid.uuid4().hex[:8]}"
        r = self.client.post("/api/auth/register", json={"username": user, "password": "web123456"})
        self.check("注册", r.status_code == 201)
        token = r.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"

        endpoints = [
            ("GET", "/api/auth/me"),
            ("GET", "/api/users/me/model-config"),
            ("GET", "/api/problems"),
            ("GET", "/api/problems/tags"),
            ("GET", "/api/judge"),
            ("GET", "/api/recommend/next"),
            ("GET", "/api/recommend/explain"),
            ("GET", "/api/recommend/roadmap"),
            ("GET", "/api/review/queue"),
            ("GET", "/api/review/due"),
            ("GET", "/api/mastery/overview"),
            ("GET", "/api/mastery/profile"),
            ("GET", "/api/mastery/timeline"),
            ("GET", "/api/wrong-book"),
            ("GET", "/api/agent"),
            ("GET", "/api/agent/stats"),
            ("GET", "/api/tasks"),
            ("GET", "/api/submissions"),
            ("GET", "/api/stats/global"),
            ("GET", "/api/data/export"),
        ]
        bad: list[str] = []
        for method, path in endpoints:
            rr = self.client.request(method, path)
            if rr.status_code >= 400:
                bad.append(f"{method} {path} -> {rr.status_code}")
        self.check(
            "前端调用的接口全部可用",
            not bad,
            f"{len(endpoints)} 个" + (f"，失败 {bad}" if bad else ""),
        )

        # ---------- 4. 做题链路（前端核心流程） ----------
        r = self.client.post(
            "/api/problems",
            json={
                "title": f"前端验收题-{uuid.uuid4().hex[:6]}",
                "statement": "输入 a b 输出 a+b",
                "tags": ["模拟"],
                "auto_annotate": True,
            },
        )
        self.check("新建题目", r.status_code == 201)
        pid = r.json()["id"]

        r = self.client.get(f"/api/problems/{pid}")
        self.check("题目详情", r.status_code == 200 and r.json()["statement"])

        r = self.client.put(
            f"/api/problems/{pid}/testcases",
            json={"replace": True, "cases": [{"input": "1 2\n", "output": "3\n", "hidden": False}]},
        )
        self.check("配置用例", r.status_code == 200 and r.json()["total"] == 1)

        r = self.client.get(f"/api/problems/{pid}/testcases")
        self.check("用例列表", r.status_code == 200 and len(r.json()["items"]) == 1)

        has_python = "python" in (self.client.get("/api/judge").json().get("languages") or [])
        if has_python:
            r = self.client.post(
                "/api/submissions",
                json={
                    "problem_id": pid,
                    "code": "a, b = map(int, input().split())\nprint(a + b)\n",
                    "language": "python",
                    "judge_mode": "sandbox",
                    "run_agents": False,
                },
            )
            body = r.json()
            self.check(
                "做题页提交并判题",
                r.status_code == 201 and body["verdict"] == "AC",
                f"verdict={body.get('verdict')}",
            )
            self.check(
                "响应含前端需要的字段",
                all(k in body for k in ("submission", "judge_mode", "verdict", "judge_detail")),
            )
        else:
            self.notes.append("本机无 python，跳过真实判题")

        # ---------- 5. 流式接口（Agent 控制台） ----------
        events: list[str] = []
        cur = None
        done_data = None
        with self.client.stream(
            "POST",
            "/api/agent/stream",
            json={"agent": "judge", "payload": {"verdict": "TLE", "code": "x"}},
        ) as resp:
            self.check("流式接口可用", resp.status_code == 200)
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    cur = line.split(":", 1)[1].strip()
                    events.append(cur)
                elif line.startswith("data:") and cur == "done":
                    done_data = json.loads(line[5:].strip()).get("result")
        self.check(
            "流式事件序列完整",
            "start" in events and "token" in events and "field" in events and "done" in events,
            f"token={events.count('token')} field={events.count('field')}",
        )
        self.check(
            "流式可产出结构化结果",
            bool(done_data and done_data.get("data")),
            f"status={done_data.get('status') if done_data else '-'}",
        )

        # ---------- 6. 后台任务（做题页链路进度） ----------
        r = self.client.post(
            "/api/agent/pipeline",
            json={
                "submission": {"problem_id": pid, "code": "print(0)", "verdict": "WA"},
                "include_roadmap": True,
            },
        )
        self.check("后台任务提交", r.status_code == 202 and bool(r.json().get("task_id")))
        task_id = r.json()["task_id"] if r.status_code == 202 else ""

        if task_id:
            task_events: list[str] = []
            with self.client.stream("GET", f"/api/agent/stream?task_id={task_id}") as resp:
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        task_events.append(line.split(":", 1)[1].strip())
                    if "done" in task_events or "failed" in task_events:
                        break
            self.check(
                "任务进度事件",
                "progress" in task_events and "done" in task_events,
                f"{task_events}",
            )
            rr = self.client.get(f"/api/tasks/{task_id}")
            self.check("任务结果可读", rr.json()["status"] == "done")

        # ---------- 7. 复习（复习页） ----------
        r = self.client.post("/api/review/mark", json={"problem_id": pid, "rating": 3})
        self.check(
            "复习标记与调度",
            r.status_code == 200 and r.json()["next_interval_days"] > 0,
            f"间隔={r.json().get('next_interval_days') if r.status_code == 200 else '-'}天",
        )
        if r.status_code == 200:
            curve = r.json().get("forgetting_curve") or []
            self.check("遗忘曲线数据可用（画像页图表）", len(curve) == 31)

        # ---------- 8. 掌握度（画像页） ----------
        self.client.post("/api/mastery/recalc")
        r = self.client.get("/api/mastery/profile")
        self.check("掌握度画像数据", r.status_code == 200)
        r = self.client.get("/api/mastery/overview")
        self.check(
            "学习状态总览",
            r.status_code == 200 and 0 <= r.json()["score"] <= 100,
            f"score={r.json().get('score')} level={r.json().get('level')}",
        )

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
