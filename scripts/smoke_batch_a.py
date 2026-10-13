"""批次 A 端到端验收：真实 HTTP 跑通"出题 → 配用例 → 判题 → Agent → 流式"。

用法（先启动服务）：
    python scripts/smoke_batch_a.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
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
        print("== 批次 A 端到端验收 ==\n")

        # 准备账号
        user = f"batcha{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            "/api/auth/register", json={"username": user, "password": "batch123456"}
        )
        self.check("注册账号", r.status_code == 201)
        self.client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        # 判题后端能力
        r = self.client.get("/api/judge")
        info = r.json()
        self.check(
            "判题后端信息",
            r.status_code == 200 and "languages" in info,
            f"backend={info.get('backend')} isolation={info.get('isolation')} langs={info.get('languages')}",
        )
        has_python = "python" in (info.get("languages") or [])
        if info.get("warnings"):
            self.notes.append(f"工具链警告: {info['warnings']}")

        # 出题
        r = self.client.post(
            "/api/problems",
            json={
                "title": f"A+B 问题-{uuid.uuid4().hex[:6]}",
                "statement": "给定两个整数 a b，输出它们的和。",
                "constraints": "|a|,|b| <= 10^9",
                "tags": ["模拟"],
                "auto_annotate": True,
            },
        )
        self.check("新建题目", r.status_code == 201)
        pid = r.json()["id"]

        # 配置测试用例
        r = self.client.put(
            f"/api/problems/{pid}/testcases",
            json={
                "replace": True,
                "cases": [
                    {"input": "1 2\n", "output": "3\n", "hidden": False},
                    {"input": "100 200\n", "output": "300\n", "hidden": True},
                    {"input": "-5 5\n", "output": "0\n", "hidden": True},
                ],
            },
        )
        self.check(
            "配置测试用例",
            r.status_code == 200 and r.json()["total"] == 3,
            f"total={r.json().get('total') if r.status_code == 200 else '-'}",
        )

        # 隐藏用例不回显
        r = self.client.get(f"/api/problems/{pid}/testcases")
        items = r.json()["items"]
        hidden_leaked = [c for c in items if c["hidden"] and c["input_data"]]
        self.check(
            "隐藏用例内容不外泄",
            len(hidden_leaked) == 0,
            f"{sum(1 for c in items if c['hidden'])} 个隐藏用例",
        )

        if has_python:
            # 正确代码 -> AC
            r = self.client.post(
                "/api/submissions",
                json={
                    "problem_id": pid,
                    "language": "python",
                    "code": "a, b = map(int, input().split())\nprint(a + b)\n",
                    "judge_mode": "sandbox",
                    "run_agents": False,
                },
            )
            ok = r.status_code == 201 and r.json()["verdict"] == "AC"
            self.check(
                "沙箱判题：正确代码 AC",
                ok,
                f"verdict={r.json().get('verdict')} passed={r.json().get('submission', {}).get('passed_cases')}",
            )

            # 错误代码 -> WA
            r = self.client.post(
                "/api/submissions",
                json={
                    "problem_id": pid,
                    "language": "python",
                    "code": "a, b = map(int, input().split())\nprint(a - b)\n",
                    "judge_mode": "sandbox",
                    "run_agents": False,
                    "dedupe": False,
                },
            )
            self.check(
                "沙箱判题：错误代码 WA",
                r.json()["verdict"] == "WA",
                f"verdict={r.json().get('verdict')}",
            )

            # 死循环 -> TLE
            r = self.client.post(
                "/api/submissions",
                json={
                    "problem_id": pid,
                    "language": "python",
                    "code": "while True:\n    pass\n",
                    "judge_mode": "sandbox",
                    "run_agents": False,
                    "dedupe": False,
                },
            )
            self.check(
                "沙箱判题：死循环 TLE",
                r.json()["verdict"] == "TLE",
                f"verdict={r.json().get('verdict')}",
            )

            # 语法错误 -> CE
            r = self.client.post(
                "/api/submissions",
                json={
                    "problem_id": pid,
                    "language": "python",
                    "code": "def broken(\n",
                    "judge_mode": "sandbox",
                    "run_agents": False,
                    "dedupe": False,
                },
            )
            self.check(
                "沙箱判题：语法错误 CE",
                r.json()["verdict"] == "CE",
                f"verdict={r.json().get('verdict')}",
            )
        else:
            self.notes.append("本机无 python 运行时，跳过沙箱判题用例")

        # 错误代码 + Agent 归因
        r = self.client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "language": "python",
                "code": "a, b = map(int, input().split())\nprint(a * b)\n",
                "judge_mode": "sandbox" if has_python else "caller",
                "verdict": "WA",
                "run_agents": True,
                "dedupe": False,
            },
        )
        body = r.json()
        self.check(
            "判题 + Agent 归因联动",
            r.status_code == 201 and body["judge"] is not None,
            f"root_cause={body.get('judge', {}).get('root_cause') if body.get('judge') else '-'}",
        )

        # 幂等键
        h = {"Idempotency-Key": "batcha-fixed-key"}
        p = {
            "problem_id": pid,
            "code": "print(7)\n",
            "judge_mode": "caller",
            "verdict": "WA",
            "run_agents": False,
        }
        a = self.client.post("/api/submissions", json=p, headers=h).json()
        b = self.client.post("/api/submissions", json=p, headers=h).json()
        self.check(
            "幂等键复用提交",
            b["deduplicated"] is True and a["submission"]["id"] == b["submission"]["id"],
        )

        # 去重
        p2 = {**p, "code": "print(8)\n", "dedupe": True}
        c = self.client.post("/api/submissions", json=p2).json()
        d = self.client.post("/api/submissions", json=p2).json()
        self.check("窗口期去重", c["deduplicated"] is False and d["deduplicated"] is True)

        # 流式建题
        r = self.client.post(
            "/api/problems",
            json={
                "title": f"流式题-{uuid.uuid4().hex[:6]}",
                "statement": "给定 n 个数，求最大值。",
                "constraints": "1 <= n <= 10^5",
                "tags": ["模拟"],
                "auto_annotate": True,
            },
        )
        self.check("流式场景建题", r.status_code == 201)

        # token 级流式
        events: list[str] = []
        cur_agent = None
        done_result = None
        with self.client.stream(
            "POST",
            "/api/agent/stream",
            json={
                "agent": "solution",
                "payload": {"title": "A+B", "statement": "输出 a+b", "tags": ["模拟"]},
            },
        ) as resp:
            self.check(
                "流式接口返回 SSE",
                resp.status_code == 200
                and "text/event-stream" in resp.headers.get("content-type", ""),
            )
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    cur_agent = line.split(":", 1)[1].strip()
                    events.append(cur_agent)
                elif line.startswith("data:") and cur_agent == "done":
                    done_result = json.loads(line[5:].strip()).get("result")
        self.check(
            "流式事件齐全",
            "token" in events and "field" in events and "done" in events,
            f"统计: token={events.count('token')} field={events.count('field')}",
        )
        self.check(
            "流式最终结果有效",
            bool(done_result and done_result.get("data", {}).get("code")),
            f"status={done_result.get('status') if done_result else '-'}",
        )

        # 任务并发上限信息
        r = self.client.get("/api/agent/stats")
        st = r.json()
        self.check(
            "运行统计含并发上限",
            "concurrency_limit" in st.get("runtime", {}),
            f"limit={st.get('runtime', {}).get('concurrency_limit')}",
        )

        # 接口总数
        r = self.client.get("/openapi.json")
        count = sum(len(m) for m in r.json()["paths"].values())
        self.check("接口数量 >= 50", count >= 50, f"实际 {count} 个")

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
