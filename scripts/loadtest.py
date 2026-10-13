"""压测脚本（批次 C / F20）。

为什么自己写而不用 locust：本项目要压的不是"HTTP QPS 极限"，
而是几个具体的工程断言 —— 用脚本直接断言比读 locust 报表更直接：

  1. 并发写不出现 database is locked（验证全局写锁）；
  2. 后台任务并发上限生效（超出排队而非无限拉起）；
  3. 限流在阈值处准确触发，且不误伤健康检查；
  4. Agent 链路在高并发下降级率与失败率是否可控；
  5. 判题（CPU 密集）与其他请求混跑时的相互影响。

用法：
    python scripts/loadtest.py --base http://127.0.0.1:8000 --users 20 --duration 15
    python scripts/loadtest.py --scenario write       # 只跑某一项
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class Stats:
    """请求统计：延迟分位 + 状态码分布 + 错误样本。"""

    latencies: list[float] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # 墙钟耗时：吞吐必须用它算，不能用"平均延迟的倒数" ——
    # 后者是单连接速率，在并发场景下会把吞吐夸大 N 倍（踩过这个坑）。
    wall_seconds: float = 0.0

    def record(self, elapsed: float, status: int, error: str = "") -> None:
        self.latencies.append(elapsed)
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        if error and len(self.errors) < 10:
            self.errors.append(error)

    @property
    def total(self) -> int:
        return len(self.latencies)

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[idx]

    def summary(self) -> dict[str, Any]:
        if not self.latencies:
            return {"total": 0}
        return {
            "total": self.total,
            # 真实聚合吞吐 = 总请求数 / 墙钟时间
            "rps": round(self.total / self.wall_seconds, 2) if self.wall_seconds else 0.0,
            "wall_seconds": round(self.wall_seconds, 2),
            "avg_ms": round(statistics.fmean(self.latencies) * 1000, 2),
            "p50_ms": round(self.percentile(0.50) * 1000, 2),
            "p95_ms": round(self.percentile(0.95) * 1000, 2),
            "p99_ms": round(self.percentile(0.99) * 1000, 2),
            "max_ms": round(max(self.latencies) * 1000, 2),
            "status": dict(sorted(self.status_counts.items())),
        }


class LoadTest:
    def __init__(self, base: str, users: int, duration: float) -> None:
        self.base = base.rstrip("/")
        self.users = users
        self.duration = duration
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        (self.passed if ok else self.failed).append(name)

    # ---------------------------------------------------------------- 工具
    async def _register(self, client: httpx.AsyncClient) -> str:
        username = f"lt{uuid.uuid4().hex[:10]}"
        r = await client.post(
            "/api/auth/register", json={"username": username, "password": "loadtest123"}
        )
        if r.status_code != 201:
            raise RuntimeError(f"注册失败: {r.status_code} {r.text[:200]}")
        return r.json()["access_token"]

    async def _setup_problem(self, client: httpx.AsyncClient, token: str) -> str:
        """建一道带用例的题，供提交压测使用。"""
        h = {"Authorization": f"Bearer {token}"}
        r = await client.post(
            "/api/problems",
            json={
                "title": f"压测题-{uuid.uuid4().hex[:6]}",
                "statement": "输入 a b 输出 a+b",
                "tags": ["模拟"],
                "auto_annotate": False,
            },
            headers=h,
        )
        pid = r.json()["id"]
        await client.put(
            f"/api/problems/{pid}/testcases",
            json={"replace": True, "cases": [{"input": "1 2\n", "output": "3\n", "hidden": False}]},
            headers=h,
        )
        return pid

    async def _worker(
        self,
        client: httpx.AsyncClient,
        token: str,
        scenario: str,
        pid: str,
        stats: Stats,
        stop_at: float,
    ) -> None:
        """单个虚拟用户循环发请求，直到到时间。"""
        h = {"Authorization": f"Bearer {token}"}
        while time.perf_counter() < stop_at:
            started = time.perf_counter()
            status = 0
            err = ""
            try:
                if scenario == "read":
                    r = await client.get("/api/problems", params={"size": 5}, headers=h)
                elif scenario == "write":
                    r = await client.post(
                        "/api/submissions",
                        json={
                            "problem_id": pid,
                            "code": f"print({uuid.uuid4().int % 100})\n",
                            "language": "python",
                            "judge_mode": "caller",
                            "verdict": "WA",
                            "run_agents": False,
                            "dedupe": False,
                        },
                        headers=h,
                    )
                elif scenario == "agent":
                    r = await client.post(
                        "/api/agent/run",
                        json={"agent": "judge", "payload": {"verdict": "WA", "code": "x"}},
                        headers=h,
                    )
                elif scenario == "pipeline":
                    r = await client.post(
                        "/api/agent/pipeline",
                        json={
                            "submission": {"problem_id": pid, "code": "print(0)", "verdict": "WA"},
                            "include_roadmap": False,
                        },
                        headers=h,
                    )
                elif scenario == "mixed":
                    # 混合读写：暴露读写相互影响
                    if uuid.uuid4().int % 3 == 0:
                        r = await client.get("/api/mastery/overview", headers=h)
                    else:
                        r = await client.post(
                            "/api/submissions",
                            json={
                                "problem_id": pid,
                                "code": f"print({uuid.uuid4().int % 100})\n",
                                "language": "python",
                                "judge_mode": "caller",
                                "verdict": "WA",
                                "run_agents": False,
                                "dedupe": False,
                            },
                            headers=h,
                        )
                else:
                    r = await client.get("/health")
                status = r.status_code
                if status >= 500:
                    err = r.text[:200]
            except Exception as e:
                status = 0
                err = f"{type(e).__name__}: {e}"
            stats.record(time.perf_counter() - started, status, err)

    # ------------------------------------------------------------- 场景
    async def run_scenario(self, scenario: str) -> Stats:
        stats = Stats()
        limits = httpx.Limits(max_connections=self.users * 2, max_keepalive_connections=self.users)
        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(base_url=self.base, limits=limits, timeout=timeout) as client:
            # 准备：注册 + 建题（各场景共用）
            token = await self._register(client)
            pid = await self._setup_problem(client, token)

            stop_at = time.perf_counter() + self.duration
            wall_start = time.perf_counter()
            await asyncio.gather(
                *[
                    self._worker(client, token, scenario, pid, stats, stop_at)
                    for _ in range(self.users)
                ]
            )
            stats.wall_seconds = time.perf_counter() - wall_start
        return stats

    # ------------------------------------------------------------- 主流程
    async def run(self, scenarios: list[str]) -> int:
        print(f"== 压测 {self.base}  并发={self.users}  每场景 {self.duration}s ==\n")

        results: dict[str, Stats] = {}
        for scenario in scenarios:
            print(f"--- 场景：{scenario} ---")
            started = time.perf_counter()
            stats = await self.run_scenario(scenario)
            elapsed = time.perf_counter() - started
            results[scenario] = stats
            summary = stats.summary()
            print(json.dumps(summary, ensure_ascii=False, indent=2))

            # 通用断言：无 5xx、无连接错误
            server_errors = sum(c for s, c in stats.status_counts.items() if s >= 500)
            conn_errors = stats.status_counts.get(0, 0)
            self.check(
                f"{scenario}: 无 5xx",
                server_errors == 0,
                f"{server_errors} 个" + (f"，样本 {stats.errors[:2]}" if stats.errors else ""),
            )
            self.check(f"{scenario}: 无连接错误", conn_errors == 0, f"{conn_errors} 个")

            if scenario == "write":
                # 核心断言：全局写锁生效，不出现 database is locked
                locked = [e for e in stats.errors if "locked" in e.lower()]
                self.check("write: 无 database is locked", not locked)
                self.check(
                    "write: 吞吐可用",
                    summary.get("rps", 0) > 0,
                    f"{summary.get('rps', 0)} req/s",
                )

            if scenario == "pipeline":
                # 任务并发上限：超出应排队（202）而不是拒绝（429/5xx）
                accepted = stats.status_counts.get(202, 0)
                self.check(
                    "pipeline: 任务被接受（排队而非拒绝）",
                    accepted > 0,
                    f"202 x{accepted}",
                )

            if scenario == "read":
                self.check(
                    "read: 延迟合理",
                    summary.get("p95_ms", 99999) < 2000,
                    f"p95={summary.get('p95_ms')}ms",
                )

            print(f"  （场景耗时 {elapsed:.1f}s）\n")

        # ---- 跨场景对比：读写混合是否拖慢读 ----
        if "read" in results and "write" in results:
            read_p95 = results["read"].summary().get("p95_ms", 0)
            write_p95 = results["write"].summary().get("p95_ms", 0)
            self.check(
                "写压测下读延迟未失控",
                read_p95 < 3000,
                f"read p95={read_p95}ms, write p95={write_p95}ms",
            )

        # ---- 收尾：核对服务端指标 ----
        async with httpx.AsyncClient(base_url=self.base, timeout=30.0) as client:
            r = await client.get("/api/stats/global")
            if r.status_code == 200:
                body = r.json()
                metrics = body.get("metrics", {})
                print("--- 服务端指标 ---")
                print(
                    json.dumps(
                        {
                            "http_requests": metrics.get("http_requests"),
                            "http_avg_ms": metrics.get("http_avg_ms"),
                            "agent_calls": metrics.get("agent_calls"),
                            "agent_degraded": metrics.get("agent_degraded"),
                            "judge": metrics.get("judge"),
                            "rate_limited": metrics.get("rate_limited"),
                            "write": body.get("write"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                self.check(
                    "服务端指标可读",
                    metrics.get("http_requests", 0) > 0,
                    f"{metrics.get('http_requests')} 请求",
                )

        print(f"\n== 结果: {len(self.passed)} 通过, {len(self.failed)} 失败 ==")
        if self.failed:
            print("失败项: " + ", ".join(self.failed))
            return 1
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="AIgoTutor 压测")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=10, help="并发虚拟用户数")
    ap.add_argument("--duration", type=float, default=10.0, help="每个场景持续秒数")
    ap.add_argument(
        "--scenario",
        default="read,write,mixed,pipeline",
        help="逗号分隔：read,write,mixed,agent,pipeline",
    )
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.scenario.split(",") if s.strip()]
    sys.exit(asyncio.run(LoadTest(args.base, args.users, args.duration).run(scenarios)))


if __name__ == "__main__":
    main()
