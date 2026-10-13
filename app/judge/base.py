"""判题后端抽象：统一契约，subprocess 与 docker 两种实现可互换。

设计要点：
  * Verdict 是全系统唯一的判定口径，与 schemas.JudgeOut 的枚举一致；
  * JudgeResult 同时携带"每个测试点的结果"，便于前端展示与错题归因；
  * 资源限制（超时/内存）由各后端自行落实，但语义必须一致：
      - 超过 deadline         -> TLE
      - 超过内存上限           -> MLE
      - 非零退出码             -> RE
      - 编译失败               -> CE
      - 输出不符               -> WA
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import settings

Verdict = Literal["AC", "WA", "TLE", "MLE", "RE", "CE", "OLE", "IE"]

LANGUAGE_SPECS: dict[str, dict[str, Any]] = {
    "python": {
        "ext": ".py",
        # 解释型语言没有编译阶段，但语法错误必须判 CE 而不是 RE，
        # 因此这里用 ast.parse 做预检（见 subprocess_backend.SYNTAX_CHECK）。
        "compile": None,
        "run": ["python", "{src}"],
        "probe": "python",
        "docker_image": "python:3.13-slim",
    },
    "cpp": {
        "ext": ".cpp",
        "compile": ["g++", "-O2", "-std=c++17", "-o", "{exe}", "{src}"],
        "run": ["{exe}"],
        "probe": "g++",
        "docker_image": "gcc:13",
    },
    "java": {
        "ext": ".java",
        # Java 要求 public class 名与文件名严格一致（大小写敏感）。
        # 类名约定为 Main，因此文件名必须是 Main.java —— 若用小写 main.java，
        # javac 会报"类 Main 是公共的，应在 Main.java 文件中声明"。
        # Windows 文件系统不区分大小写会掩盖这个问题，Linux 上直接失败。
        "filename": "Main.java",
        # 编译产物输出到 classes/ 子目录：若与源文件同目录，javac 在处理
        # -d 与源路径时会冲突。
        "compile": ["javac", "-encoding", "UTF-8", "-d", "{outdir}", "{src}"],
        "run": ["java", "-cp", "{outdir}", "Main"],
        "probe": "javac",
        "docker_image": "eclipse-temurin:21-jdk",
    },
}


@dataclass
class TestCase:
    """一个测试点。hidden=True 的用例不下发给用户。

    注意：类名以 Test 开头会被 pytest 误当作测试类收集（它会尝试实例化），
    因此在本文件末尾设置了 `TestCase.__test__ = False` 提示 pytest 跳过。
    """

    id: str = ""
    input_data: str = ""
    expected_output: str = ""
    hidden: bool = False
    score: int = 10


@dataclass
class JudgeRequest:
    language: str
    code: str
    cases: list[TestCase]
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    problem_id: str = ""


@dataclass
class CaseResult:
    case_id: str
    verdict: Verdict
    time_ms: int = 0
    memory_kb: int = 0
    stdout: str = ""
    stderr: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "verdict": self.verdict,
            "time_ms": self.time_ms,
            "memory_kb": self.memory_kb,
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:2000],
            "message": self.message,
        }


@dataclass
class JudgeResult:
    verdict: Verdict
    passed: int = 0
    total: int = 0
    time_ms: int = 0
    memory_kb: int = 0
    cases: list[CaseResult] = field(default_factory=list)
    compile_output: str = ""
    backend: str = ""
    message: str = ""

    @property
    def score(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.passed / self.total * 100.0, 2)

    def first_failure(self) -> CaseResult | None:
        for c in self.cases:
            if c.verdict != "AC":
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "passed": self.passed,
            "total": self.total,
            "score": self.score,
            "time_ms": self.time_ms,
            "memory_kb": self.memory_kb,
            "backend": self.backend,
            "message": self.message,
            "compile_output": self.compile_output[:2000],
            "cases": [c.to_dict() for c in self.cases],
        }


def resolve_toolchain(language: str) -> list[str]:
    """解析语言运行/编译命令，规避 PATH 里工具版本错配。

    真实踩坑：Windows 上 javac 来自 JDK 17，而 PATH 里 Oracle 的 javapath
    把 java 指向 JRE 8，于是编译产物 class 版本 61 在 JRE 8 上直接
    UnsupportedClassVersionError。竞赛判题系统必须自己解决这个问题。

    策略：若 javac 目录下存在同名 java，优先用它（同一个 JDK，版本必然匹配）。
    """
    import shutil

    resolved: dict[str, str] = {}
    for tool in ("python", "g++", "javac", "java"):
        path = shutil.which(tool)
        if path:
            resolved[tool] = path

    spec = LANGUAGE_SPECS.get(language)
    if spec is None:
        return []

    cmd = list(spec["compile"] or spec["run"])
    # Java 特例：compile 用 javac，run 用 java —— 让 java 跟随 javac
    if language == "java" and "javac" in resolved:
        from pathlib import Path as _P

        javac = _P(resolved["javac"])
        sibling = javac.with_name("java.exe" if os.name == "nt" else "java")
        if sibling.exists():
            resolved["java"] = str(sibling)
    return cmd


# 各语言的工具就绪检查：返回 (可用语言列表, 问题说明)
def probe_language(language: str) -> tuple[bool, str]:
    """探测语言工具链是否可用，以及是否存在版本错配。"""
    import shutil
    import subprocess

    spec = LANGUAGE_SPECS.get(language)
    if spec is None:
        return False, f"不支持的语言: {language}"

    if not shutil.which(spec["probe"]):
        return False, f"未找到 {spec['probe']}"

    if language == "java":
        javac = shutil.which("javac")
        java = shutil.which("java")
        if not java:
            return False, "未找到 java 运行时"
        from pathlib import Path as _P

        sibling = _P(javac).with_name("java.exe" if os.name == "nt" else "java")
        java_bin = str(sibling) if sibling.exists() else java
        try:
            cv = subprocess.run([javac, "-version"], capture_output=True, text=True, timeout=10)
            rv = subprocess.run([java_bin, "-version"], capture_output=True, text=True, timeout=10)
            cver = (cv.stdout + cv.stderr).strip()
            raw_rv = (rv.stdout + rv.stderr).strip()
            rver = raw_rv.splitlines()[0] if raw_rv else ""

            import re as _re

            def _major(text: str) -> int:
                """从两种格式里取主版本号。

                javac 输出:  javac 17.0.14        （无引号）
                java  输出:  java version "1.8.0_341" 或 openjdk version "17.0.14"
                """
                m = _re.search(r'version\s+"(\d+)(?:\.(\d+))?', text)
                if m:
                    first = int(m.group(1))
                    return first if first > 1 else int(m.group(2) or 0)
                m = _re.search(r"javac\s+(\d+)", text)
                if m:
                    return int(m.group(1))
                return 0

            cm, rm = _major(cver), _major(rver)
            # 只有两边都成功解析出主版本、且确实不同才告警，
            # 避免解析失败（0 vs 0）产生误报。
            if cm and rm and cm != rm:
                return True, (
                    f"javac({cver}) 与 java({rver}) 主版本不一致，"
                    "编译产物可能无法运行；建议让两者来自同一个 JDK"
                )
        except Exception:
            pass
    return True, ""


def strip_bom(text: str) -> str:
    """剥掉 UTF-8 BOM。

    真实痛点：Windows 下从剪贴板/记事本粘贴的代码常带 BOM，Java 编译器会直接
    报 "非法字符: '\\ufeff'" 而判 CE，用户百思不得其解。写文件前统一剥掉。
    """
    if text and text[0] == "\ufeff":
        return text[1:]
    return text


def normalize_output(text: str) -> str:
    """输出比对归一：忽略行尾空格与末尾空行（竞赛通用口径）。"""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


class JudgeBackend(ABC):
    name: str = "base"

    @abstractmethod
    async def judge(self, req: JudgeRequest) -> JudgeResult: ...

    @abstractmethod
    def available_languages(self) -> list[str]: ...

    def availability(self) -> dict[str, Any]:
        langs = self.available_languages()
        return {
            "backend": self.name,
            "available": bool(langs),
            "languages": langs,
            "reason": "" if langs else "没有任何可用的语言运行时",
        }


class NullBackend(JudgeBackend):
    """不判题：未配置后端或运行时缺失时使用，明确报错而非静默通过。"""

    name = "none"

    async def judge(self, req: JudgeRequest) -> JudgeResult:
        return JudgeResult(
            verdict="IE",
            total=len(req.cases),
            backend=self.name,
            message="未启用判题后端（AIGOTUTOR_JUDGE_BACKEND=none），无法真实判题",
        )

    def available_languages(self) -> list[str]:
        return []


def language_available(language: str) -> bool:
    """探测本机是否有对应运行时/编译器 —— 缺了就明确报错，不静默失败。"""
    spec = LANGUAGE_SPECS.get(language)
    if spec is None:
        return False
    return shutil.which(spec["probe"]) is not None


_cache: dict[str, JudgeBackend] = {}


# 告诉 pytest：这不是测试类，别收集它（否则会尝试实例化并报 PytestCollectionWarning）
TestCase.__test__ = False  # type: ignore[attr-defined]


def build_backend(name: str) -> JudgeBackend:
    key = (name or "none").lower()
    if key == "subprocess":
        from .subprocess_backend import SubprocessBackend

        return SubprocessBackend()
    if key == "docker":
        from .docker_backend import DockerBackend

        return DockerBackend()
    return NullBackend()


def get_backend() -> JudgeBackend:
    name = settings.JUDGE_BACKEND
    if name not in _cache:
        _cache[name] = build_backend(name)
    return _cache[name]


def reset_backend_cache() -> None:
    """测试用：切换后端配置后清缓存。"""
    _cache.clear()


def describe_backend() -> dict[str, Any]:
    backend = get_backend()
    return backend.availability()
