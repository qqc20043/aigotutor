"""DockerBackend 的单元测试（mock docker CLI，不依赖真实 Docker）。

目标：填补 docker_backend.py 的 0% 覆盖空白（全仓库确凿空白）。
策略：探测与命令构造不 mock 数据结构、只 mock 外壳进程；judge 主流程
通过 mock _exec 验证编排逻辑（编译失败短路 / 用例循环 / 汇总）。
"""

from __future__ import annotations

import shutil
from unittest.mock import patch

import pytest

from app.judge.base import JudgeRequest, TestCase
from app.judge.docker_backend import DOCKER_PROBE_TIMEOUT, DockerBackend
from app.judge.subprocess_backend import SYNTAX_CHECK


@pytest.fixture
def backend() -> DockerBackend:
    return DockerBackend()


# ============================================================ 探测
def test_docker_ok_false_when_binary_missing(backend):
    """机器上没有 docker 二进制 -> 探测失败（不抛异常）。"""
    with patch("shutil.which", return_value=None):
        assert backend._docker_ok() is False


def test_docker_ok_false_when_info_fails(backend):
    """docker info 返回非零（守护进程未起）-> 探测失败。"""
    import subprocess

    with patch("shutil.which", return_value="/usr/bin/docker"):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"err")):
            assert backend._docker_ok() is False


def test_docker_ok_true_when_info_succeeds(backend):
    import subprocess

    with patch("shutil.which", return_value="/usr/bin/docker"):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as m:
            assert backend._docker_ok() is True
            assert m.call_args.args[0][1] == "info"
            assert m.call_args.kwargs.get("timeout") == DOCKER_PROBE_TIMEOUT


def test_available_languages_empty_when_docker_unavailable(backend):
    """Docker 不可用时语言列表必须为空（而不是带着不可用的后端继续）。"""
    with patch.object(backend, "_docker_ok", return_value=False):
        assert backend.available_languages() == []


def test_availability_reports_strong_isolation(backend):
    with patch.object(backend, "_docker_ok", return_value=True):
        info = backend.availability()
    assert info["backend"] == "docker"
    assert info["isolation"] == "strong"
    assert info["available"] is True
    assert "python:3.13-slim" in info["image"]


def test_probe_timeout_constant_is_bounded():
    """探测超时必须有界（防 docker info 挂死拖垮管理页）。"""
    assert 0 < DOCKER_PROBE_TIMEOUT <= 10


# ============================================================ 命令构造
def test_docker_base_hardening_flags(backend, tmp_path):
    """docker run 必须带隔离加固旗标：断网/剥权/只读限制/内存/CPU。"""
    cmd = backend._docker_base(tmp_path, mem_mb=128, timeout_s=5.0)
    joined = " ".join(cmd)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 128" in joined
    assert "--memory 128m" in joined and "--memory-swap 128m" in joined
    assert "--cpus 1" in joined
    assert f"-v {tmp_path}:/box:rw" in joined


def test_docker_base_uses_workdir_mount(backend, tmp_path):
    """挂载点必须指向临时工作目录 /box（容器内只见提交代码）。"""
    cmd = backend._docker_base(tmp_path, mem_mb=64, timeout_s=1.0)
    assert any("/box" in part for part in cmd)


# ============================================================ judge 编排
def _patch_exec(backend, script):
    """mock _exec：script 是 (code, out, err) 序列，按调用次序弹出。"""
    calls: list[list[str]] = []

    async def fake_exec(cmd, timeout, stdin=""):
        calls.append(cmd)
        return script.pop(0) if script else (0, "", "")

    return calls, fake_exec


async def test_judge_ie_when_docker_unavailable(backend):
    """Docker 不可用必须判 IE 并说明原因（不静默降级到本机执行）。"""
    with patch.object(backend, "_docker_ok", return_value=False):
        result = await backend.judge(
            JudgeRequest(language="python", code="print(1)", cases=[TestCase(id="c1", input_data="", expected_output="1")])
        )
    assert result.verdict == "IE"
    assert "Docker" in result.message or "docker" in result.message


async def test_judge_ie_for_unknown_language(backend):
    with patch.object(backend, "_docker_ok", return_value=True):
        result = await backend.judge(
            JudgeRequest(language="brainfuck", code="+++", cases=[TestCase(id="c1")])
        )
    assert result.verdict == "IE"
    assert "brainfuck" in result.message


async def test_judge_ie_without_cases(backend):
    with patch.object(backend, "_docker_ok", return_value=True):
        result = await backend.judge(JudgeRequest(language="python", code="print(1)", cases=[]))
    assert result.verdict == "IE"
    assert "用例" in result.message


async def test_judge_compile_failure_short_circuits(backend, tmp_path):
    """编译失败必须判 CE 并停止（不再逐用例运行）。"""
    with patch.object(backend, "_docker_ok", return_value=True):
        with patch.object(backend, "_exec", return_value=(1, "", "SyntaxError: bad")):
            result = await backend.judge(
                JudgeRequest(
                    language="python",
                    code="def broken(",
                    cases=[TestCase(id="c1", input_data="", expected_output="1")],
                )
            )
    assert result.verdict == "CE"
    assert "编译失败" in result.message
    assert result.compile_output and "SyntaxError" in result.compile_output


async def test_judge_happy_path_all_cases_pass(backend, tmp_path):
    """编译成功 + 两个用例输出匹配 -> AC，逐用例结果正确。"""
    script = [(0, "", ""), (0, "3\n", ""), (0, "300\n", "")]
    with patch.object(backend, "_docker_ok", return_value=True):
        with patch.object(backend, "_exec", side_effect=lambda cmd, timeout, stdin="": script.pop(0)):
            result = await backend.judge(
                JudgeRequest(
                    language="python",
                    code="a,b=map(int,input().split());print(a+b)",
                    cases=[
                        TestCase(id="c1", input_data="1 2\n", expected_output="3\n"),
                        TestCase(id="c2", input_data="100 200\n", expected_output="300\n"),
                    ],
                )
            )
    assert result.verdict == "AC"
    assert result.passed == 2 and result.total == 2
    assert all(c.verdict == "AC" for c in result.cases)


async def test_judge_first_case_fails_verdict_wa(backend, tmp_path):
    """任一用例不匹配 -> 整体 WA（首个失败用例的 verdict 决定整体）。"""
    script = [(0, "", ""), (0, "2\n", ""), (0, "999\n", "")]
    with patch.object(backend, "_docker_ok", return_value=True):
        with patch.object(backend, "_exec", side_effect=lambda cmd, timeout, stdin="": script.pop(0)):
            result = await backend.judge(
                JudgeRequest(
                    language="python",
                    code="x",
                    cases=[
                        TestCase(id="c1", input_data="1 2\n", expected_output="3\n"),
                        TestCase(id="c2", input_data="1 2\n", expected_output="300\n"),
                    ],
                )
            )
    assert result.verdict == "WA"


async def test_judge_java_uses_main_filename(backend, tmp_path):
    """Java 的源文件名必须是 Main.java（类名约束），编译命令含 outdir。"""
    calls, fake_exec = _patch_exec(backend, [(0, "", ""), (0, "3\n", "")])
    with patch.object(backend, "_docker_ok", return_value=True):
        with patch.object(backend, "_exec", side_effect=fake_exec):
            await backend.judge(
                JudgeRequest(
                    language="java",
                    code="public class Main {}",
                    cases=[TestCase(id="c1", input_data="", expected_output="3\n")],
                )
            )
    # 编译命令里应引用 /box/Main.java 与 /box/classes
    compile_cmd = " ".join(calls[0])
    assert "/box/Main.java" in compile_cmd
    assert "/box/classes" in compile_cmd


async def test_python_syntax_check_present():
    """python 必须有语法预检命令（解释型语言错误要判 CE 而非 RE）。"""
    assert "python" in SYNTAX_CHECK
    assert any("ast" in part for part in SYNTAX_CHECK["python"])


# ============================================================ _exec 超时
async def test_exec_timeout_returns_negative_code(backend, tmp_path):
    """_exec 超时必须杀进程并返回 -9（TLE 语义），不悬挂。"""

    # 用一个真实但永不出结果的进程模拟超时（python -c sleep）
    python = shutil.which("python") or shutil.which("python3")
    if not python:  # pragma: no cover - 环境无 python 不可能
        pytest.skip("无 python")
    code, _out, err = await backend._exec(
        [python, "-c", "import time; time.sleep(30)"], timeout=1.0
    )
    assert code == -9
    assert "超时" in err
