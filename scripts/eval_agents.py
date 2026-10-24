"""Agent 行为评估 CLI：跑 golden set 并产出 Markdown/JSON 报告。

用法：
    # mock 全量（本地快速回归，与 CI 同口径）
    python scripts/eval_agents.py

    # 真模型抽检（provider/网关可从环境变量继承）
    python scripts/eval_agents.py --provider openai

    # 只跑部分 Agent；对比提示词效果时用 --fresh 跳过结果缓存
    python scripts/eval_agents.py --only hint,review

环境变量（优先级：命令行 > AIGOTUTOR_* > LLM_* 漫思茶网关命名）：
    AIGOTUTOR_PROVIDER / AIGOTUTOR_MODEL / AIGOTUTOR_BASE_URL / AIGOTUTOR_API_KEY
    LLM_BASE_URL / LLM_API_KEY / LLM_CHAT_MODEL

输出：evals/reports/eval-<时间戳>.md 与 .json（目录不入库，报告进 docs 执行记录）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evals import run_eval


def _first_env(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            return v
    return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 行为评估（golden set + 规则断言）")
    parser.add_argument("--provider", default=None, help="mock | openai | anthropic | gemini")
    parser.add_argument("--model", default=None, help="模型名（如 deepseek-v4.1-flash）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容网关地址")
    parser.add_argument("--api-key", default=None, help="API Key（建议走环境变量）")
    parser.add_argument("--only", default="", help="只跑这些 Agent，逗号分隔（hint,review,...）")
    parser.add_argument("--out", default="evals/reports", help="报告输出目录")
    args = parser.parse_args()

    provider = args.provider or _first_env("AIGOTUTOR_PROVIDER", default="mock")
    model = args.model or _first_env("AIGOTUTOR_MODEL", "LLM_CHAT_MODEL", default="mock-1")
    if provider == "mock":
        model = "mock-1"  # mock 不读模型名；报告里不该出现网关模型名造成误解
    config = {
        "provider": provider,
        "model": model,
        "base_url": args.base_url or _first_env("AIGOTUTOR_BASE_URL", "LLM_BASE_URL", default=""),
        "api_key": args.api_key or _first_env("AIGOTUTOR_API_KEY", "LLM_API_KEY", default=""),
    }
    only = [x.strip() for x in args.only.split(",") if x.strip()]

    print("== Agent 行为评估 ==")
    print(
        f"provider={config['provider']} model={config['model']} "
        f"base_url={config['base_url'] or '(默认)'} only={only or '全部'}"
    )
    report = asyncio.run(run_eval(config, only or None))

    print()
    print(report.to_markdown())

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = outdir / f"eval-{stamp}.md"
    json_path = outdir / f"eval-{stamp}.json"
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"报告已写入: {md_path}")
    print(f"          {json_path}")

    return 0 if report.passed_count == len(report.results) else 1


if __name__ == "__main__":
    sys.exit(main())
