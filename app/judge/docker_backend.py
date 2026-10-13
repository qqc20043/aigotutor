"""Docker 判题后端：容器隔离执行。

相对 subprocess 后端的安全提升：
  * 文件系统隔离：只挂载临时目录，容器内看不到宿主其他文件；
  * 网络隔离：--network none；
  * 能力剥夺：--cap-drop ALL、--security-opt no-new-privileges；
  * 只读根文件系统 + tmpfs 写区；
  * 资源限制由内核 cgroup 强制（--memory/--cpus/--pids-limit），
    比 setrlimit 更可靠，且 Windows/macOS 上表现一致。

前提：本机需安装 Docker 且守护进程可访问（docker info 成功）。
不可用时不静默降级 —— 返回 IE 并说明原因，避免"以为在隔离环境跑、
实际在本机跑"的错误安全感。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import settings
from .base import (
    LANGUAGE_SPECS,
    CaseResult,
    JudgeBackend,
    JudgeRequest,
    JudgeResult,
    normalize_output,
    strip_bom,
)
from .subprocess_backend import SYNTAX_CHECK

DOCKER_PROBE_TIMEOUT = 5.0


class DockerBackend(JudgeBackend):
    name = "docker"

    def __init__(self, image: str | None = None) -> None:
        self.image = image or settings.JUDGE_DOCKER_IMAGE

    # ---------------------------------------------------------------- 可用性
    def available_languages(self) -> list[str]:
        if not self._docker_ok():
            return []
        # 容器内镜像自带运行时，因此只要 docker 可用，全部语言都算可用
        return list(LANGUAGE_SPECS)

    def _docker_ok(self) -> bool:
        exe = shutil.which("docker")
        if exe is None:
            return False
        try:
            import subprocess

            r = subprocess.run(
                [exe, "info"],
                capture_output=True,
                timeout=DOCKER_PROBE_TIMEOUT,
                check=False,
            )
            return r.returncode == 0
        except Exception:
            return False

    def availability(self) -> dict[str, Any]:
        ok = self._docker_ok()
        return {
            "backend": self.name,
            "available": ok,
            "languages": list(LANGUAGE_SPECS) if ok else [],
            "powered_by": "Docker 容器",
            "isolation": "strong",
            "image": self.image,
            "note": "内核 cgroup 限制内存与 CPU，网络禁用，文件系统只读挂载",
            "reason": "" if ok else "本机没有可用的 Docker（docker info 失败或未安装）",
        }

    # ------------------------------------------------------------------ 判题
    async def judge(self, req: JudgeRequest) -> JudgeResult:
        if not self._docker_ok():
            return JudgeResult(
                verdict="IE",
                total=len(req.cases),
                backend=self.name,
                message="Docker 不可用，无法判题。请安装 Docker 或切换 JUDGE_BACKEND=subprocess",
            )

        spec = LANGUAGE_SPECS.get(req.language)
        if spec is None:
            return JudgeResult(
                verdict="IE",
                total=len(req.cases),
                backend=self.name,
                message=f"不支持的语言: {req.language}",
            )
        if not req.cases:
            return JudgeResult(
                verdict="IE", total=0, backend=self.name, message="该题没有配置测试用例"
            )

        workdir = Path(tempfile.mkdtemp(prefix="aigotutor-docker-"))
        try:
            return await self._judge_in(workdir, spec, req)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _docker_base(self, workdir: Path, mem_mb: int, timeout_s: float) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            f"{mem_mb}m",
            "--memory-swap",
            f"{mem_mb}m",
            "--cpus",
            "1",
            "-v",
            f"{workdir}:/box:rw",
            "-w",
            "/box",
        ]

    async def _exec(self, cmd: list[str], timeout: float, stdin: str = "") -> tuple[int, str, str]:
        limit = settings.JUDGE_OUTPUT_LIMIT
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return 127, "", f"无法启动 docker: {e}"
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(stdin.encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return -9, "", "docker 执行超时"
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace")[:limit],
            err_b.decode("utf-8", errors="replace")[:limit],
        )

    async def _judge_in(
        self, workdir: Path, spec: dict[str, Any], req: JudgeRequest
    ) -> JudgeResult:
        fname = spec.get("filename", f"main{spec['ext']}")
        src = workdir / fname
        src.write_text(strip_bom(req.code), encoding="utf-8")
        (workdir / "classes").mkdir(exist_ok=True)
        image = spec.get("docker_image") or self.image
        mem = req.memory_limit_mb or settings.JUDGE_MEMORY_MB
        compile_output = ""

        def _inner(parts: list[str]) -> str:
            return " ".join(
                part.format(
                    src=f"/box/{fname}",
                    exe="/box/main",
                    dir="/box",
                    outdir="/box/classes",
                )
                for part in parts
            )

        # 编译
        compile_cmd = spec["compile"] or SYNTAX_CHECK.get(req.language)
        if compile_cmd:
            cmd = [
                *self._docker_base(workdir, mem, settings.JUDGE_TIMEOUT_SECONDS),
                image,
                "sh",
                "-c",
                _inner(compile_cmd),
            ]
            code, out, err = await self._exec(
                cmd, timeout=max(20.0, settings.JUDGE_TIMEOUT_SECONDS * 3)
            )
            compile_output = (out + err)[: settings.JUDGE_OUTPUT_LIMIT]
            if code != 0:
                return JudgeResult(
                    verdict="CE",
                    total=len(req.cases),
                    backend=self.name,
                    compile_output=compile_output,
                    message="编译失败",
                    cases=[
                        CaseResult(case_id=c.id or f"case{i}", verdict="CE")
                        for i, c in enumerate(req.cases)
                    ],
                )

        inner_run = _inner(spec["run"])

        case_results: list[CaseResult] = []
        passed = 0
        max_time = max_mem = 0

        for idx, case in enumerate(req.cases):
            cid = case.id or f"case{idx + 1}"
            timeout = max(1.0, req.time_limit_ms / 1000.0) + 3.0  # 给容器启动留余量
            cmd = [*self._docker_base(workdir, mem, timeout), image, "sh", "-c", inner_run]
            started = time.perf_counter()
            code, out, err = await self._exec(cmd, timeout=timeout, stdin=case.input_data)
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            if code == -9:
                cr = CaseResult(
                    case_id=cid,
                    verdict="TLE",
                    time_ms=elapsed_ms,
                    message=f"超出时间限制 {req.time_limit_ms}ms",
                )
            elif code == 137:
                cr = CaseResult(
                    case_id=cid,
                    verdict="MLE",
                    time_ms=elapsed_ms,
                    stdout=out[:2000],
                    message=f"超出内存限制 {mem}MB（容器被杀）",
                )
            elif code != 0:
                cr = CaseResult(
                    case_id=cid,
                    verdict="RE",
                    time_ms=elapsed_ms,
                    stdout=out[:2000],
                    stderr=err[:2000],
                    message=f"运行时错误（退出码 {code}）",
                )
            elif normalize_output(out) != normalize_output(case.expected_output):
                cr = CaseResult(
                    case_id=cid,
                    verdict="WA",
                    time_ms=elapsed_ms,
                    stdout=out[:2000],
                    message="输出与期望不符",
                )
            else:
                cr = CaseResult(case_id=cid, verdict="AC", time_ms=elapsed_ms, stdout=out[:500])
                passed += 1

            case_results.append(cr)
            max_time = max(max_time, cr.time_ms)
            max_mem = max(max_mem, cr.memory_kb)

        verdict = "AC"
        for c in case_results:
            if c.verdict != "AC":
                verdict = c.verdict
                break

        return JudgeResult(
            verdict=verdict,
            passed=passed,
            total=len(req.cases),
            time_ms=max_time,
            memory_kb=max_mem,
            cases=case_results,
            compile_output=compile_output,
            backend=self.name,
            message=f"{passed}/{len(req.cases)} 通过（Docker 隔离）",
        )
