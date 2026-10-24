"""本机子进程判题后端。

安全说明：这不是强安全沙箱（见包级 docstring）。约束手段：
  1. 每次运行在独立临时目录，跑完即删；
  2. 硬超时 kill 进程树；
  3. Unix 用 setrlimit 限制 CPU 与地址空间；Windows 无等价能力，
     退化为"超时 + 事后峰值内存检查"，并在 availability() 中如实标注；
  4. stdout/stderr 输出截断，防 OLE 打爆内存。

跨平台差异是真实存在的，这里选择"如实降级 + 明示"而不是假装一致。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..config import settings
from .base import (
    LANGUAGE_SPECS,
    CaseResult,
    JudgeBackend,
    JudgeRequest,
    JudgeResult,
    TestCase,
    language_available,
    normalize_output,
    probe_language,
    strip_bom,
)

IS_WINDOWS = sys.platform == "win32"
IS_UNIX = not IS_WINDOWS


def loop_supports_subprocess() -> tuple[bool, str]:
    """当前事件循环是否支持创建子进程。

    为什么需要这个检查：Windows 上 SelectorEventLoop 不支持子进程，
    create_subprocess_exec 会直接抛 NotImplementedError（裸异常对用户
    毫无指引）。典型触发场景是 `uvicorn app.main:app --reload` ——
    reload 会让 uvicorn 选用 Selector 循环，判题随即全部 500。

    run.py 已显式钉住 Proactor 规避此问题；这里再做一道运行时防线，
    保证"任何启动方式"下失败都是**可读的报错**而不是栈溢出。
    """
    if not IS_WINDOWS:
        return True, ""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 无运行中的循环（如单元测试直接调用）
        return True, ""
    if isinstance(loop, asyncio.ProactorEventLoop):
        return True, ""
    return False, (
        "当前事件循环不支持子进程（Windows 的 SelectorEventLoop），判题无法执行。"
        "请用 `python run.py` 启动（已自动选择 Proactor 循环），"
        "或改为 `uvicorn app.main:app` 且不要加 --reload/--workers。"
    )


# 需要显式"预编译检查"的解释型语言：它们没有编译阶段，
# 语法错误会以非零退出码在运行时暴露，若不做预检就会被误判为 RE。
SYNTAX_CHECK: dict[str, list[str]] = {
    "python": [
        "python",
        "-W",
        "ignore",
        "-c",
        "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())",
        "{src}",
    ],
}


def _looks_like_memory_error(stderr: str) -> bool:
    """stderr 是否呈现"内存分配失败"特征。

    Unix 用 RLIMIT_AS 限内存时，超限进程不是被外部 kill，而是自己分配失败后
    异常退出。各语言的表征不同，这里逐一匹配：

      Python  MemoryError
      C++     std::bad_alloc / terminate called after throwing
      Java    java.lang.OutOfMemoryError

    两种平台都要用：Unix 上是 RLIMIT_AS 的"分配即失败"退出；Windows 上
    进程会自己抛 MemoryError/OutOfMemoryError 退出（采样监控只负责
    "换页拖死被 kill"的情形）。匹配不到就按普通 RE 处理 —— 宁可漏判成 RE，
    也不能把真正的运行时错误误报为 MLE。
    """
    if not stderr:
        return False
    markers = (
        "MemoryError",
        "std::bad_alloc",
        "OutOfMemoryError",
        "Cannot allocate memory",
        "bad_alloc",
    )
    return any(m in stderr for m in markers)


class SubprocessBackend(JudgeBackend):
    name = "subprocess"

    def available_languages(self) -> list[str]:
        return [lang for lang in LANGUAGE_SPECS if language_available(lang)]

    def _resolve_cmd(self, parts: list[str]) -> list[str]:
        """解析命令中的工具路径，修正 PATH 版本错配。

        Java 特例：PATH 上 javac(JDK17) 与 java(JRE8) 可能来自不同安装，
        导致编译产物无法运行。这里让 java 跟随 javac 所在目录。
        """
        first = parts[0]
        if first == "java":
            javac = shutil.which("javac")
            if javac:
                sibling = Path(javac).with_name("java.exe" if IS_WINDOWS else "java")
                if sibling.exists():
                    parts = [str(sibling), *parts[1:]]
        return parts

    def availability(self) -> dict[str, Any]:
        langs = self.available_languages()
        info = super().availability()
        info["powered_by"] = "本机子进程"
        info["isolation"] = "weak"  # 明示隔离强度
        info["note"] = "仅适用于本地开发与内网可信场景；公网部署请切换 JUDGE_BACKEND=docker"

        # 事件循环不支持子进程时（Windows + reload 的经典组合），
        # 语言探测会显示"可用"但实际一判就失败 —— 必须在此如实上报，
        # 否则用户会以为判题正常，直到提交才踩坑。
        loop_ok, loop_msg = loop_supports_subprocess()
        info["loop_supports_subprocess"] = loop_ok
        if not loop_ok:
            info["available"] = False
            info["languages"] = []
            info["reason"] = loop_msg
            return info

        if IS_WINDOWS:
            info["memory_limit"] = "sampled"
            info["note"] += "。Windows 无法用 setrlimit，内存限制为实时采样后终止"
        else:
            info["memory_limit"] = "setrlimit"

        # 版本错配预警：工具可用但组合有问题时如实上报
        warnings: list[str] = []
        for lang in langs:
            ok, msg = probe_language(lang)
            if ok and msg:
                warnings.append(f"{lang}: {msg}")
        info["warnings"] = warnings
        if not langs:
            info["reason"] = "本机未找到任何语言运行时（需要 python / g++ / javac 之一）"
        return info

    # ------------------------------------------------------------------ 主流程
    async def judge(self, req: JudgeRequest) -> JudgeResult:
        spec = LANGUAGE_SPECS.get(req.language)
        if spec is None:
            return JudgeResult(
                verdict="IE",
                total=len(req.cases),
                backend=self.name,
                message=f"不支持的语言: {req.language}",
            )
        if not language_available(req.language):
            return JudgeResult(
                verdict="IE",
                total=len(req.cases),
                backend=self.name,
                message=(
                    f"本机缺少 {req.language} 运行时（需 {spec['probe']}），"
                    "无法判题。请安装后重试或改用其他语言。"
                ),
            )
        if not req.cases:
            return JudgeResult(
                verdict="IE", total=0, backend=self.name, message="该题没有配置测试用例"
            )

        # 事件循环能力预检：不支持子进程时明确报错，不让 NotImplementedError 裸抛
        loop_ok, loop_msg = loop_supports_subprocess()
        if not loop_ok:
            return JudgeResult(
                verdict="IE",
                total=len(req.cases),
                backend=self.name,
                message=loop_msg,
            )

        workdir = Path(tempfile.mkdtemp(prefix="aigotutor-judge-"))
        try:
            return await self._judge_in(workdir, spec, req)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _judge_in(
        self, workdir: Path, spec: dict[str, Any], req: JudgeRequest
    ) -> JudgeResult:
        # 文件名优先用语言规范名（Java 的 Main.java 不能大写错），否则用 main{ext}
        src = workdir / spec.get("filename", f"main{spec['ext']}")
        # 剥 BOM：Windows 粘贴的代码常带，会让 javac 直接报非法字符
        src.write_text(strip_bom(req.code), encoding="utf-8")
        outdir = workdir / "classes"
        outdir.mkdir(exist_ok=True)

        # ---------------- 编译阶段 ----------------
        compile_output = ""
        compile_cmd = spec["compile"] or SYNTAX_CHECK.get(req.language)
        if compile_cmd:
            cmd = [
                part.format(
                    src=str(src),
                    exe=str(workdir / "main"),
                    dir=str(workdir),
                    outdir=str(outdir),
                )
                for part in compile_cmd
            ]
            cmd = self._resolve_cmd(cmd)
            code, out, err = await self._run_process(
                cmd, workdir, input_data="", timeout=max(10.0, settings.JUDGE_TIMEOUT_SECONDS)
            )
            compile_output = (out + err)[: settings.JUDGE_OUTPUT_LIMIT]
            if code != 0:
                return JudgeResult(
                    verdict="CE",
                    total=len(req.cases),
                    backend=self.name,
                    compile_output=compile_output,
                    message="编译失败" if spec["compile"] else "语法检查未通过",
                    cases=[
                        CaseResult(case_id=c.id or f"case{i}", verdict="CE")
                        for i, c in enumerate(req.cases)
                    ],
                )

        # ---------------- 运行阶段 ----------------
        run_cmd = [
            part.format(
                src=str(src),
                exe=str(workdir / "main"),
                dir=str(workdir),
                outdir=str(outdir),
            )
            for part in spec["run"]
        ]
        run_cmd = self._resolve_cmd(run_cmd)
        if req.language == "java" and req.memory_limit_mb > 0:
            # JVM 与 RLIMIT_AS 天生不兼容（见 _run_process_with_resources 的
            # _limits 说明），内存上限改由 -Xmx 交给 JVM 自己执行；
            # RSS 采样监控仍作兜底。
            run_cmd.insert(1, f"-Xmx{req.memory_limit_mb}m")

        case_results: list[CaseResult] = []
        passed = 0
        max_time = 0
        max_mem = 0

        for idx, case in enumerate(req.cases):
            cid = case.id or f"case{idx + 1}"
            result = await self._run_case(run_cmd, workdir, case, cid, req)
            case_results.append(result)
            max_time = max(max_time, result.time_ms)
            max_mem = max(max_mem, result.memory_kb)
            if result.verdict == "AC":
                passed += 1

        verdict = "AC" if passed == len(req.cases) else case_results[0].verdict
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
            message=f"{passed}/{len(req.cases)} 通过",
        )

    async def _run_case(
        self,
        cmd: list[str],
        workdir: Path,
        case: TestCase,
        case_id: str,
        req: JudgeRequest,
    ) -> CaseResult:
        timeout = max(0.5, req.time_limit_ms / 1000.0)
        mem_limit_mb = req.memory_limit_mb or settings.JUDGE_MEMORY_MB
        started = time.perf_counter()
        code, out, err, peak_kb = await self._run_process_with_resources(
            cmd, workdir, case.input_data, timeout, mem_limit_mb, language=req.language
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # 内存超限优先判定：Windows 上超限进程会被换页拖死，
        # 若先判超时会误报 TLE（实测踩到过，-11 是监控器主动杀的标记）
        if code == -11 or (peak_kb and peak_kb > mem_limit_mb * 1024):
            return CaseResult(
                case_id=case_id,
                verdict="MLE",
                time_ms=elapsed_ms,
                memory_kb=peak_kb,
                stdout=out[:2000],
                message=f"内存 {peak_kb // 1024}MB 超出限制 {mem_limit_mb}MB",
            )
        if code == -9 or elapsed_ms > req.time_limit_ms + 200:
            return CaseResult(
                case_id=case_id,
                verdict="TLE",
                time_ms=elapsed_ms,
                memory_kb=peak_kb,
                stdout=out[:2000],
                message=f"超出时间限制 {req.time_limit_ms}ms",
            )
        if len(out) >= settings.JUDGE_OUTPUT_LIMIT:
            return CaseResult(
                case_id=case_id,
                verdict="OLE",
                time_ms=elapsed_ms,
                memory_kb=peak_kb,
                stdout=out[:2000],
                message="输出过大",
            )
        if code != 0:
            # 内存分配失败有两种表征：Unix 上 RLIMIT_AS 是"分配即失败"语义，
            # 进程自己因 MemoryError 异常退出；Windows 上进程会自己抛
            # MemoryError/OutOfMemoryError 退出（采样监控器只负责"换页拖死
            # 被 kill"的情形，已在上面判过）。两种平台都必须按 MLE 判 ——
            # 用户明明写的是内存超限的代码，却被告知"运行时错误"完全误导。
            if mem_limit_mb > 0 and _looks_like_memory_error(err):
                return CaseResult(
                    case_id=case_id,
                    verdict="MLE",
                    time_ms=elapsed_ms,
                    memory_kb=peak_kb,
                    stdout=out[:2000],
                    stderr=err[:2000],
                    message=f"内存超出限制 {mem_limit_mb}MB（分配失败退出）",
                )
            return CaseResult(
                case_id=case_id,
                verdict="RE",
                time_ms=elapsed_ms,
                memory_kb=peak_kb,
                stdout=out[:2000],
                stderr=err[:2000],
                message=f"运行时错误（退出码 {code}）",
            )

        if normalize_output(out) != normalize_output(case.expected_output):
            return CaseResult(
                case_id=case_id,
                verdict="WA",
                time_ms=elapsed_ms,
                memory_kb=peak_kb,
                stdout=out[:2000],
                message="输出与期望不符",
            )

        return CaseResult(
            case_id=case_id,
            verdict="AC",
            time_ms=elapsed_ms,
            memory_kb=peak_kb,
            stdout=out[:500],
        )

    # -------------------------------------------------------------- 进程执行
    async def _run_process(
        self, cmd: list[str], cwd: Path, input_data: str, timeout: float
    ) -> tuple[int, str, str]:
        code, out, err, _ = await self._run_process_with_resources(cmd, cwd, input_data, timeout, 0)
        return code, out, err

    async def _run_process_with_resources(
        self,
        cmd: list[str],
        cwd: Path,
        input_data: str,
        timeout: float,
        mem_limit_mb: int,
        language: str = "",
    ) -> tuple[int, str, str, int]:
        """执行命令，返回 (退出码, stdout, stderr, 峰值内存KB)。

        内存限制的两条路径：
          * Unix: setrlimit(RLIMIT_AS) 由内核强制，分配失败即异常退出；
          * Windows: 无等价 API，靠后台采样监控 —— 一旦超限立刻 kill，
            否则进程会陷入疯狂换页，最后撞上超时被误判为 TLE（实测踩到过）。
        例外：java 两边都不走内核限制 —— JVM 启动时要一次性保留最大堆的
        虚拟地址空间（默认 ≈ 物理内存 1/4），RLIMIT_AS 会直接把 JVM 掐死在
        启动阶段（CI 实测 7ms 判 RE）；其内存上限由 -Xmx（见 _judge_in）+
        RSS 采样监控承担。
        """
        limit = settings.JUDGE_OUTPUT_LIMIT

        def _limits():  # pragma: no cover - 仅 Unix 生效
            if not IS_UNIX or mem_limit_mb <= 0:
                return
            try:
                import resource

                # java 跳过 AS/NPROC：JVM 启动要保留数 GB 虚拟地址空间（远超
                # 内存上限），GC/JIT 线程组也会撞 NPROC，见上方 docstring。
                enforce_rlimits = language != "java"
                if enforce_rlimits:
                    bytes_limit = mem_limit_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
                cpu = int(max(1.0, timeout)) + 1
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
                if enforce_rlimits:
                    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
            except (ValueError, OSError, ImportError):
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                preexec_fn=_limits if IS_UNIX else None,
            )
        except (OSError, ValueError) as e:
            return 127, "", f"无法启动进程: {e}", 0

        peak_kb = 0
        mem_exceeded = False

        async def _watch_memory() -> None:
            """后台采样内存；超限即杀，返回后由调用方判定 MLE。

            杀进程用异步版本：同步 taskkill 会阻塞事件循环，让并发的
            判题请求互相拖死。
            """
            nonlocal peak_kb, mem_exceeded
            if mem_limit_mb <= 0:
                return
            try:
                import psutil  # type: ignore
            except ImportError:
                return
            try:
                parent = psutil.Process(proc.pid)
            except Exception:
                return
            threshold = mem_limit_mb * 1024
            while proc.returncode is None:
                try:
                    total = parent.memory_info().rss // 1024
                    for child in parent.children(recursive=True):
                        with suppress(Exception):
                            total += child.memory_info().rss // 1024
                    peak_kb = max(peak_kb, total)
                    if total > threshold:
                        mem_exceeded = True
                        await self._kill_tree_async(proc.pid)
                        return
                except Exception:
                    return
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(_watch_memory())
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(input_data.encode("utf-8")), timeout=timeout
            )
            code = proc.returncode if proc.returncode is not None else 0
        except TimeoutError:
            # 先杀进程树，再收尸。
            # 注意：收集输出与 wait() 都必须带超时 —— Windows 上被杀进程的
            # 管道可能不关闭，裸 wait() 会把事件循环挂死（实测踩到 40s 卡死）。
            peak_kb = max(peak_kb, await self._kill_tree_async(proc.pid))
            await self._reap(proc)
            if mem_exceeded:
                return -11, "", "内存超限后被终止", peak_kb
            return -9, "", "执行超时", peak_kb
        except Exception as e:
            await self._kill_tree_async(proc.pid)
            await self._reap(proc)
            return 1, "", f"执行异常: {e}", 0
        finally:
            if not watcher.done():
                watcher.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await watcher

        if mem_exceeded:
            return -11, "", "内存超限", peak_kb

        return (
            code,
            out_b.decode("utf-8", errors="replace")[:limit],
            err_b.decode("utf-8", errors="replace")[:limit],
            peak_kb,
        )

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process, timeout: float = 3.0) -> None:
        """回收已杀进程，带超时兜底，绝不无限等。"""
        with suppress(TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        # 关闭管道，避免 ResourceWarning 与句柄泄漏
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            with suppress(Exception):
                if stream is not None:
                    stream.close()

    async def _kill_tree_async(self, pid: int) -> int:
        """异步杀进程树，避免同步 taskkill 阻塞事件循环。

        返回杀之前采到的峰值内存 KB（拿不到则为 0）。
        先用 psutil 递归杀（跨平台），再用系统命令补刀 —— psutil 的 kill
        之后子进程仍可能残留，而残留的判题进程会继续吃 CPU 与内存。
        """
        peak = 0
        try:
            import psutil  # type: ignore

            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for p in [*children, parent]:
                with suppress(Exception):
                    peak = max(peak, p.memory_info().rss // 1024)
            for p in [*children, parent]:
                with suppress(Exception):
                    p.kill()
            await self._force_kill(pid)
            return peak
        except Exception:
            pass

        # 没有 psutil（或进程已消失）时的兜底
        await self._force_kill(pid)
        return 0

    @staticmethod
    async def _force_kill(pid: int) -> None:
        """系统级强制终止：Windows 用 taskkill 带 /T 杀整棵树，Unix 用 SIGKILL。"""
        with suppress(Exception):
            if IS_WINDOWS:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    ),
                    timeout=8.0,
                )
            else:
                os.kill(pid, 9)
