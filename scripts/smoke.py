"""真实 HTTP 冒烟测试：对已启动的服务打真实请求，验证完整用户旅程。

用法（先启动服务）：
    python scripts/smoke.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import uuid

import httpx


class Smoke:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=60.0)
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)

    def run(self) -> int:
        print(f"== 冒烟测试目标: {self.base} ==\n")

        # 1) 健康检查
        r = self.client.get("/health")
        self.check("健康检查", r.status_code == 200 and r.json()["db"] is True)

        # 2) 注册
        username = f"smoke{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "smoke123456", "nickname": "冒烟"},
        )
        self.check("注册", r.status_code == 201, f"user={username}")
        token = r.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"

        # 3) 当前用户
        r = self.client.get("/api/auth/me")
        self.check("读取当前用户", r.status_code == 200 and r.json()["username"] == username)

        # 4) 配置模型（mock）
        r = self.client.put(
            "/api/users/me/model-config",
            json={"provider": "mock", "model": "mock-1", "purpose": "default"},
        )
        self.check("写入模型配置", r.status_code == 200)

        # 5) 新建题目（自动两阶段标注）
        r = self.client.post(
            "/api/problems",
            json={
                "title": f"冒烟题-区间合并-{uuid.uuid4().hex[:6]}",
                "statement": "给定 n 个闭区间，合并所有重叠区间并输出。n <= 10^5。",
                "constraints": "1 <= n <= 10^5",
                "tags": ["排序", "贪心"],
                "auto_annotate": True,
            },
        )
        self.check(
            "新建题目+自动标注", r.status_code == 201, f"difficulty={r.json().get('difficulty')}"
        )
        pid = r.json()["id"]

        # 6) 题目详情
        r = self.client.get(f"/api/problems/{pid}")
        self.check("题目详情", r.status_code == 200 and r.json()["statement"])

        # 7) 成本路由预览
        r = self.client.post(f"/api/problems/{pid}/route")
        self.check("成本路由预览", r.status_code == 200 and "use_llm" in r.json())

        # 8) 提交（带 Agent 判错归因）
        r = self.client.post(
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
        self.check(
            "提交+判错归因",
            r.status_code == 201 and bool(r.json()["judge"]["root_cause"]),
            f"root_cause={r.json()['judge']['root_cause'] if r.status_code == 201 else '-'}",
        )
        sid = r.json()["submission"]["id"]

        # 9) 生成题解
        r = self.client.post(f"/api/submissions/{sid}/solution")
        ok = r.status_code == 200 and bool(r.json()["data"]["code"])
        self.check("生成结构化题解", ok)

        # 10) 提交详情（含落库的评审）
        r = self.client.get(f"/api/submissions/{sid}")
        self.check(
            "提交详情",
            r.status_code == 200 and len(r.json()["reviews"]) >= 1,
            f"reviews={len(r.json()['reviews']) if r.status_code == 200 else 0}",
        )

        # 11) 推荐
        r = self.client.get("/api/recommend/next")
        self.check(
            "下一题推荐",
            r.status_code == 200 and bool(r.json()["reason"]),
            f"reason={r.json()['reason'][:40] if r.status_code == 200 else '-'}",
        )

        # 12) 推荐可解释性
        r = self.client.get("/api/recommend/explain")
        self.check("推荐可解释性", r.status_code == 200 and len(r.json()["factors"]) == 3)

        # 13) 路线图
        r = self.client.get("/api/recommend/roadmap", params={"weeks": 6})
        self.check("学习路线图", r.status_code == 200 and len(r.json()["stages"]) >= 1)

        # 14) 掌握度总览
        r = self.client.get("/api/mastery/overview")
        ok = r.status_code == 200 and 0 <= r.json()["score"] <= 100
        self.check("掌握度总览", ok, f"score={r.json().get('score')} level={r.json().get('level')}")

        # 15) 掌握度重算 + 时间线
        r = self.client.post("/api/mastery/recalc")
        self.check("掌握度重算", r.status_code == 200)
        r = self.client.get("/api/mastery/timeline")
        self.check("状态时间线", r.status_code == 200 and len(r.json()["items"]) >= 1)

        # 16) 复习队列 + 标记
        r = self.client.get("/api/review/queue")
        self.check("复习队列", r.status_code == 200)
        r = self.client.post("/api/review/mark", json={"problem_id": pid, "rating": 3})
        ok = r.status_code == 200 and r.json()["schedule"]["interval_days"] > 0
        self.check(
            "标记复习(FSRS)",
            ok,
            f"下次间隔={r.json()['schedule']['interval_days'] if r.status_code == 200 else '-'}天",
        )
        r = self.client.get("/api/review/due")
        self.check("今日待复习+预测", r.status_code == 200 and len(r.json()["forecast"]) == 31)

        # 17) 错题本 + 聚类
        r = self.client.get("/api/wrong-book")
        self.check("错题本", r.status_code == 200 and len(r.json()["items"]) >= 1)
        r = self.client.post("/api/wrong-book/cluster")
        self.check(
            "易错题聚类",
            r.status_code == 200 and r.json()["count"] >= 1,
            f"簇数={r.json().get('count')}",
        )

        # 18) Agent 列表与单跑
        r = self.client.get("/api/agent")
        self.check("Agent 契约", r.status_code == 200 and len(r.json()["core_agents"]) == 5)
        r = self.client.post(
            "/api/agent/run",
            json={
                "agent": "judge",
                "payload": {"verdict": "TLE", "code": "for i in range(10**9): pass"},
            },
        )
        ok = r.status_code == 200 and r.json()["data"]["root_cause"] == "复杂度"
        self.check(
            "单 Agent 直跑",
            ok,
            f"root_cause={r.json()['data']['root_cause'] if r.status_code == 200 else '-'}",
        )

        # 19) 缓存命中（成本控制）
        payload = {"agent": "judge", "payload": {"verdict": "WA", "code": "print(1)"}}
        self.client.post("/api/agent/run", json=payload)
        r = self.client.post("/api/agent/run", json=payload)
        ok = r.status_code == 200 and r.json()["cached"] is True and r.json()["tokens"] == 0
        self.check("结果缓存命中", ok)

        # 20) 后台任务 + SSE 流式
        r = self.client.post(
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
        task_id = r.json()["task_id"] if r.status_code == 202 else ""
        self.check("提交后台任务", r.status_code == 202 and bool(task_id), f"task_id={task_id}")

        events: list[str] = []
        if task_id:
            with self.client.stream("GET", f"/api/agent/stream?task_id={task_id}") as resp:
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    if "done" in events or "failed" in events:
                        break
            self.check("SSE 事件流", "progress" in events and "done" in events, f"事件={events}")

            r = self.client.get(f"/api/tasks/{task_id}")
            body = r.json()
            ok = body["status"] == "done" and body["result"]["agents"]
            self.check(
                "任务结果",
                ok,
                f"status={body['status']} agents={list(body.get('result', {}).get('agents', {}))}",
            )

        # 21) Agent 运行统计
        r = self.client.get("/api/agent/stats")
        ok = r.status_code == 200 and "degraded_rate" in r.json()["db"]
        self.check(
            "Agent 运行统计",
            ok,
            f"降级率={r.json()['db']['degraded_rate'] if r.status_code == 200 else '-'}",
        )

        # 22) 任务列表
        r = self.client.get("/api/tasks")
        self.check("任务列表", r.status_code == 200)

        # 23) 数据导出
        r = self.client.get("/api/data/export")
        ok = r.status_code == 200 and "mastery" in r.json()
        self.check("数据导出", ok)

        # 24) 接口总数
        r = self.client.get("/openapi.json")
        count = sum(len(m) for m in r.json()["paths"].values())
        self.check("接口数量 >= 27", count >= 27, f"实际 {count} 个")

        print(f"\n== 结果: {self.passed} 通过, {len(self.failed)} 失败 ==")
        if self.failed:
            print("失败项: " + ", ".join(self.failed))
            return 1
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AIgoTutor 真实 HTTP 冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    sys.exit(Smoke(args.base).run())


if __name__ == "__main__":
    main()
