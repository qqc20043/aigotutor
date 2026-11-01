"""官方题解参考代码自检：抽取每段题解的 ```python 块，逐题逐用例子进程跑。

用法：python -m scripts.verify_solutions
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CODE_RE = re.compile(r"```python\n(.*?)```", re.DOTALL)


def extract_codes(solution_md: str) -> list[str]:
    return CODE_RE.findall(solution_md)


async def main() -> int:
    from scripts.problem_pack import PROBLEMS

    fails = 0
    total = 0
    for prob in PROBLEMS:
        codes = extract_codes(prob["solution"])
        if not codes:
            print(f"✗ {prob['title']}: 题解缺 ```python 参考代码")
            fails += 1
            continue
        for ci, code in enumerate(codes):
            for si, (stdin_text, expected) in enumerate(prob["testcases"]):
                total += 1
                r = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    code,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await r.communicate(stdin_text.encode("utf-8"))
                # Windows 子进程 stdout 是 CRLF；与判题器 normalize_output 同语义
                stdout = out.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if r.returncode != 0 or stdout != expected:
                    fails += 1
                    print(
                        f"✗ {prob['title']} 代码#{ci} 用例#{si}: rc={r.returncode} "
                        f"got={stdout!r} want={expected!r} err={err.decode()[:200]}"
                    )
    print(f"参考代码自检: {total} 次运行, 失败 {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
