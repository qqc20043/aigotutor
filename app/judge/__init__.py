"""判题沙箱包。

统一抽象 JudgeBackend，两种实现：
  * SubprocessBackend —— 本机子进程执行，跨平台，演示与内网可用；
  * DockerBackend     —— 容器隔离执行，公网部署用。

安全边界（务必阅读）：
  SubprocessBackend **不是**强安全沙箱。它通过超时、内存上限、输出截断、
  临时目录隔离来约束行为，但运行的是本机进程，恶意代码理论上仍可触达
  文件系统与网络。仅适用于：本地开发、内网、可信用户的练习场景。
  面向公网必须切换 JUDGE_BACKEND=docker，或再套一层容器/虚拟机。
"""

from .base import (
    LANGUAGE_SPECS,
    CaseResult,
    JudgeBackend,
    JudgeRequest,
    JudgeResult,
    TestCase,
    Verdict,
    build_backend,
    describe_backend,
    get_backend,
    language_available,
    normalize_output,
    probe_language,
    strip_bom,
)

__all__ = [
    "LANGUAGE_SPECS",
    "CaseResult",
    "JudgeBackend",
    "JudgeRequest",
    "JudgeResult",
    "TestCase",
    "Verdict",
    "build_backend",
    "describe_backend",
    "get_backend",
    "language_available",
    "normalize_output",
    "probe_language",
    "strip_bom",
]
