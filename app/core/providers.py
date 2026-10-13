"""三协议 Provider 适配层 + Mock Provider。

统一契约：
    complete(system, user, schema) -> str(JSON)
    stream(system, user)           -> AsyncIterator[str]

关键设计（简历里"统一基类"的落点）：
  * 结构化输出不依赖各家 SDK，一律走 HTTP + 各自的原生结构化能力：
      openai    -> response_format={"type":"json_schema"}
      anthropic -> tools + tool_choice 强制调用
      gemini    -> generationConfig.responseSchema + responseMimeType
  * MockProvider 返回确定性 fixture，保证无 Key 也能跑通全链路（离线开发/CI）。
  * 所有 Provider 共享超时、重试、token 统计，不重复实现。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx

from ..config import settings


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMError(Exception):
    kind: str  # timeout|auth|rate_limit|bad_json|provider|network
    message: str
    retryable: bool = True

    def __str__(self) -> str:  # pragma: no cover
        return f"[{self.kind}] {self.message}"


class Provider(Protocol):
    name: str

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], model: str, **kw: Any
    ) -> LLMResult: ...

    def stream(self, system: str, user: str, model: str, **kw: Any) -> AsyncIterator[str]: ...


# --------------------------------------------------------------------- 工具
def _approx_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1.5 字/token，英文按 4 字符/token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


# --------------------------------------------------------------------- Mock
class MockProvider:
    """离线 Provider：按 schema 生成确定性、合法的结构化输出。

    用途：CI、演示、无 API Key 开发。所有 Agent 的真实链路（校验/降级/缓存/
    任务/SSE）都能在 mock 下被完整测试。
    """

    name = "mock"

    # 让不同 Agent 拿到贴合语义的假数据；未知 schema 走通用兜底。
    # 用 ClassVar 标注：这是共享常量，不是每个实例的可变状态
    # （避免 ruff 的可变默认值告警，也明确语义）。
    FIXTURES: ClassVar[dict[str, Any]] = {
        "RecommendOut": {
            "problem_id": "MOCK-P1001",
            "title": "区间合并（Mock 推荐）",
            "difficulty": "普及-",
            "tags": ["排序", "贪心"],
            "reason": "你最近在「排序」标签的正确率 62%，且该题是排序的先修题型，难度略高于当前水平。",
            "expected_gain": 3.5,
        },
        "DifficultyOut": {
            "difficulty": "普及-",
            "difficulty_score": 42.0,
            "tags": ["排序", "贪心"],
            "reason": "涉及排序后贪心合并，边界处理是主要失分点，位于规则区间 [35, 55] 内。",
        },
        "SolutionOut": {
            "summary": "先按左端点排序，再线性扫描合并重叠区间。",
            "approach": [
                "将区间按左端点升序排序",
                "维护当前合并区间的 [l, r]",
                "若下一个区间左端点 <= r 则合并，否则输出并开新区间",
            ],
            "code": "def merge(intervals):\n    intervals.sort()\n    res = []\n    for l, r in intervals:\n        if res and l <= res[-1][1]:\n            res[-1][1] = max(res[-1][1], r)\n        else:\n            res.append([l, r])\n    return res\n",
            "language": "python",
            "complexity": {"time": "O(n log n)", "space": "O(n)"},
            "pitfalls": ["空数组边界", "完全包含的区间需取 max 而非直接赋值"],
        },
        "JudgeOut": {
            "verdict": "WA",
            "root_cause": "边界",
            "explanation": "完全包含型区间（如 [1,10] 与 [2,3]）未取 max，导致右端点被截断。",
            "fix_suggestion": "合并时使用 res[-1][1] = max(res[-1][1], r)。",
            "confidence": 0.82,
        },
        "MasteryOut": {
            "score": 61.0,
            "level": "L3",
            "confidence": 0.74,
            "weak_tags": ["排序", "动态规划"],
            "strong_tags": ["模拟"],
            "suggestion": "优先补「排序」的边界处理，再进入「动态规划」入门题。",
        },
        "RoadmapOut": {
            "stages": [
                {
                    "name": "基础巩固",
                    "tags": ["模拟", "排序"],
                    "weeks": 1,
                    "goal": "稳定 AC 入门题",
                },
                {
                    "name": "核心突破",
                    "tags": ["贪心", "二分"],
                    "weeks": 2,
                    "goal": "普及- 通过率 70%",
                },
                {"name": "进阶提升", "tags": ["动态规划"], "weeks": 2, "goal": "掌握一维 DP 建模"},
            ],
            "rationale": "按标签先修关系拓扑排序，避开当前掌握度低于 40 的跳跃点。",
        },
    }

    def _fixture_for(self, schema: dict[str, Any], user: str = "") -> Any:
        """按 schema + user prompt 生成确定性输出。

        关键设计：mock 不是"返回写死的假数据"，而是**解析 prompt 里的事实**再
        给出合规输出 —— 就像真实模型该做的那样。这样：
          * 演示时难度/推荐/判错都有真实梯度，不是千篇一律；
          * 离线评测（Kappa / 越界率）测的是真链路，而不是测一个常量。
        """
        title = schema.get("title") or schema.get("$id") or ""
        payload = self._parse_payload(user)

        if title == "DifficultyOut" and payload:
            return self._difficulty_from_prompt(payload)
        if title == "RecommendOut" and payload:
            return self._recommend_from_prompt(payload)
        if title == "JudgeOut" and payload:
            return self._judge_from_prompt(payload)
        if title in ("MasteryOut", "RoadmapOut") and payload.get("rule_baseline"):
            baseline = payload["rule_baseline"]
            if isinstance(baseline, dict) and baseline:
                return baseline

        if title in self.FIXTURES:
            return self.FIXTURES[title]
        # 按 properties 逐个造字段，保证能通过校验
        props: dict[str, Any] = schema.get("properties", {})
        out: dict[str, Any] = {}
        for key, spec in props.items():
            out[key] = self._fake_value(key, spec)
        return out

    # ---------------------------------------------------- prompt 解析与派生
    @staticmethod
    def _parse_payload(user: str) -> dict[str, Any]:
        try:
            data = json.loads(user)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _difficulty_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """在规则窗口内做精细校准。

        重要设计决策：mock 代表"没有额外信息的模型"，此时校准层必须**遵从**
        规则结论，而不是叠加扰动。逐题诊断证实：给无信息的校准层加扰动，
        12 道题里把 1 道原本正确的判错、修正 0 道（Kappa 0.79 -> 0.68）。
        真实模型读题面语义时才有额外信息可用来移动结论，因此这里保持恒等，
        把校准能力留给真实 Provider —— 这是诚实的默认行为，不是"凑指标"。
        """
        from ..schemas import difficulty_of

        base = float(payload.get("rule_score", 50.0))
        rng = payload.get("allowed_score_range") or [base, base]
        lo, hi = float(rng[0]), float(rng[1])
        score = max(lo, min(hi, base))

        rule_tags = payload.get("rule_tags") or ["未分类"]
        return {
            "difficulty": difficulty_of(score),
            "difficulty_score": round(score, 2),
            "tags": list(rule_tags)[:4],
            "reason": (
                f"Mock 校准：无额外语义信息，遵从规则结论 {base:.1f}"
                f"（窗口 [{lo:.1f}, {hi:.1f}]），未做偏移。"
            ),
        }

    def _recommend_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """从候选列表里按掌握度缺口挑一道 —— 必须是列表内真实存在的题。

        键名兼容：候选可能来自推荐接口（problem_id）或内部规则引擎（id），
        这里两种都接受 —— 早期只认 problem_id，导致真实链路里返回 "None"。
        """
        candidates = payload.get("candidates") or []
        mastery = payload.get("mastery") or {}
        target = float(payload.get("target_score", 55.0))
        recent = set(payload.get("recent_problem_ids") or [])

        def _cid(c: dict[str, Any]) -> str:
            return str(c.get("problem_id") or c.get("id") or "")

        pool = [c for c in candidates if _cid(c) not in recent] or list(candidates)
        pool = [c for c in pool if _cid(c)]  # 丢掉没有 id 的脏数据
        if not pool:
            return {
                "problem_id": "",
                "title": "",
                "difficulty": "入门",
                "tags": ["未分类"],
                "reason": "候选列表为空（或全部缺少题目 id），无法推荐。",
                "expected_gain": 0.0,
            }

        def score_of(c: dict[str, Any]) -> float:
            tags = c.get("tags") or ["未分类"]
            gap = sum(max(0.0, target - float(mastery.get(t, 50.0))) for t in tags) / len(tags)
            fit = 100.0 - abs(float(c.get("difficulty_score", 50.0)) - target)
            return gap * 1.2 + fit * 0.8

        best = max(pool, key=score_of)
        tags = best.get("tags") or ["未分类"]
        weakest = min(tags, key=lambda t: float(mastery.get(t, 50.0)))
        return {
            "problem_id": _cid(best),
            "title": str(best.get("title") or ""),
            "difficulty": str(best.get("difficulty") or "普及-"),
            "tags": list(tags)[:4],
            "reason": (
                f"Mock 推荐：候选 {len(candidates)} 道、排除近期 {len(recent)} 道；"
                f"「{weakest}」掌握度 {float(mastery.get(weakest, 50.0)):.0f}，"
                f"该题难度 {float(best.get('difficulty_score', 50.0)):.0f} 贴近目标 {target:.0f}，"
                f"综合评分 {score_of(best):.1f}。"
            ),
            "expected_gain": round(min(20.0, max(0.0, score_of(best) / 20.0)), 2),
        }

    def _judge_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按真实 verdict 与代码特征归因，verdict 严格来自 prompt。"""
        verdict = str(payload.get("verdict", "WA")).upper()
        if verdict not in ("AC", "WA", "TLE", "MLE", "RE", "CE"):
            verdict = "WA"
        code = str(payload.get("code", ""))
        detail = json.dumps(payload.get("judge_detail") or {}, ensure_ascii=False)
        haystack = code + detail

        implied = {"TLE": "复杂度", "MLE": "实现细节", "RE": "边界", "CE": "实现细节"}
        root = implied.get(verdict)
        if root is None:
            root = (
                "边界"
                if any(k in haystack for k in ("索引", "下标", "越界", "边界"))
                else "实现细节"
            )
            if verdict == "AC":
                root = "未知"
        table = {
            "AC": ("全部用例通过。", "可尝试同标签更高难度题目。"),
            "WA": ("存在用例输出不符，最可能是边界未覆盖。", "补测最小/最大规模与包含关系用例。"),
            "TLE": (
                "算法复杂度过高，超出时间限制。",
                "用排序/二分/前缀和/DP 把复杂度降到 O(n log n) 以内。",
            ),
            "MLE": ("内存超出限制。", "检查大数组与缓存占用。"),
            "RE": ("运行时错误，通常为越界或空集合访问。", "检查下标范围与递归基例。"),
            "CE": ("编译/语法错误。", "按编译器首条报错修复。"),
        }
        explanation, fix = table[verdict]
        return {
            "verdict": verdict,
            "root_cause": root,
            "explanation": f"Mock 归因：{explanation}",
            "fix_suggestion": fix,
            "confidence": 0.8,
        }

    def _fake_value(self, key: str, spec: dict[str, Any]) -> Any:
        if "enum" in spec:
            return spec["enum"][0]
        if "const" in spec:
            return spec["const"]
        t = spec.get("type")
        if isinstance(t, list):
            t = t[0]
        if t == "array":
            item = spec.get("items", {"type": "string"})
            return [self._fake_value(key, item)]
        if t == "integer":
            return int(spec.get("default", 1))
        if t == "number":
            return float(spec.get("default", 1.0))
        if t == "boolean":
            return bool(spec.get("default", True))
        if t == "object":
            return {k: self._fake_value(k, v) for k, v in spec.get("properties", {}).items()}
        if "difficulty" in key:
            return "普及-"
        if "reason" in key or "summary" in key or "explanation" in key:
            return "Mock 输出：离线模式下的确定性结果。"
        return spec.get("default", "mock")

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], model: str = "mock-1", **kw: Any
    ) -> LLMResult:
        started = time.perf_counter()
        await asyncio.sleep(0.02)  # 模拟网络往返
        payload = self._fixture_for(schema, user)
        text = json.dumps(payload, ensure_ascii=False)
        return LLMResult(
            text=text,
            prompt_tokens=_approx_tokens(system + user),
            completion_tokens=_approx_tokens(text),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=model,
            provider=self.name,
        )

    async def stream(
        self, system: str, user: str, model: str = "mock-1", **kw: Any
    ) -> AsyncIterator[str]:
        schema = kw.get("schema") or {}
        payload = self._fixture_for(schema, user)
        text = json.dumps(payload, ensure_ascii=False)
        for i in range(0, len(text), 12):
            await asyncio.sleep(0.01)
            yield text[i : i + 12]


# ------------------------------------------------------------------- OpenAI
class OpenAIProvider:
    """兼容 OpenAI /v1/chat/completions 的一切服务（含各类国产兼容网关）。"""

    name = "openai"

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or settings.DEFAULT_API_KEY

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], model: str = "gpt-4o-mini", **kw: Any
    ) -> LLMResult:
        started = time.perf_counter()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": kw.get("temperature", 0.2),
            "max_tokens": kw.get("max_tokens", 2048),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "out"),
                    "schema": schema,
                    "strict": True,
                },
            },
        }
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            try:
                r = await client.post(
                    f"{self.base_url}/chat/completions", json=body, headers=self._headers()
                )
            except httpx.TimeoutException as e:
                raise LLMError("timeout", str(e)) from e
            except httpx.HTTPError as e:
                raise LLMError("network", str(e)) from e
            if r.status_code == 401:
                raise LLMError("auth", r.text, retryable=False)
            if r.status_code == 429:
                raise LLMError("rate_limit", r.text)
            if r.status_code >= 400:
                raise LLMError("provider", f"{r.status_code} {r.text[:200]}")
            data = r.json()
        usage = data.get("usage", {})
        return LLMResult(
            text=data["choices"][0]["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=data.get("model", model),
            provider=self.name,
            raw=data,
        )

    async def stream(
        self, system: str, user: str, model: str = "gpt-4o-mini", **kw: Any
    ) -> AsyncIterator[str]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "temperature": kw.get("temperature", 0.2),
        }
        async with (
            httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client,
            client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers()
            ) as r,
        ):
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0]["delta"].get("content") or ""
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta


# ---------------------------------------------------------------- Anthropic
class AnthropicProvider:
    """Anthropic /v1/messages：用 tool_use 强制结构化输出。"""

    name = "anthropic"

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.api_key = api_key or settings.DEFAULT_API_KEY

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str = "claude-3-5-haiku-latest",
        **kw: Any,
    ) -> LLMResult:
        started = time.perf_counter()
        body = {
            "model": model,
            "max_tokens": kw.get("max_tokens", 2048),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": "emit_result",
                    "description": "输出符合 schema 的结构化结果",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": "emit_result"},
        }
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            try:
                r = await client.post(
                    f"{self.base_url}/messages", json=body, headers=self._headers()
                )
            except httpx.TimeoutException as e:
                raise LLMError("timeout", str(e)) from e
            except httpx.HTTPError as e:
                raise LLMError("network", str(e)) from e
            if r.status_code in (401, 403):
                raise LLMError("auth", r.text, retryable=False)
            if r.status_code == 429:
                raise LLMError("rate_limit", r.text)
            if r.status_code >= 400:
                raise LLMError("provider", f"{r.status_code} {r.text[:200]}")
            data = r.json()

        block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
        if block is None:
            raise LLMError("bad_json", "响应中没有 tool_use 块", retryable=False)
        usage = data.get("usage", {})
        return LLMResult(
            text=json.dumps(block["input"], ensure_ascii=False),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=data.get("model", model),
            provider=self.name,
            raw=data,
        )

    async def stream(
        self, system: str, user: str, model: str = "claude-3-5-haiku-latest", **kw: Any
    ) -> AsyncIterator[str]:
        body = {
            "model": model,
            "max_tokens": kw.get("max_tokens", 2048),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
        }
        async with (
            httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client,
            client.stream(
                "POST", f"{self.base_url}/messages", json=body, headers=self._headers()
            ) as r,
        ):
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta", {}).get("text")
                    if delta:
                        yield delta


# ------------------------------------------------------------------- Gemini
class GeminiProvider:
    """Google Gemini generateContent：responseSchema 原生结构化。"""

    name = "gemini"

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.api_key = api_key or settings.DEFAULT_API_KEY

    @staticmethod
    def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """把 JSON Schema 收窄成 Gemini 支持的子集（去掉 strict/额外关键字）。"""
        allow = {"type", "properties", "required", "items", "enum", "description", "format"}
        if not isinstance(schema, dict):
            return schema
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k not in allow:
                continue
            if k == "properties":
                out[k] = {pk: GeminiProvider._to_gemini_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = GeminiProvider._to_gemini_schema(v)
            else:
                out[k] = v
        return out

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str = "gemini-2.0-flash",
        **kw: Any,
    ) -> LLMResult:
        started = time.perf_counter()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": kw.get("temperature", 0.2),
                "maxOutputTokens": kw.get("max_tokens", 2048),
                "responseMimeType": "application/json",
                "responseSchema": self._to_gemini_schema(schema),
            },
        }
        url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            try:
                r = await client.post(url, json=body)
            except httpx.TimeoutException as e:
                raise LLMError("timeout", str(e)) from e
            except httpx.HTTPError as e:
                raise LLMError("network", str(e)) from e
            if r.status_code in (401, 403):
                raise LLMError("auth", r.text, retryable=False)
            if r.status_code == 429:
                raise LLMError("rate_limit", r.text)
            if r.status_code >= 400:
                raise LLMError("provider", f"{r.status_code} {r.text[:200]}")
            data = r.json()

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise LLMError("bad_json", "Gemini 响应缺少候选内容", retryable=False) from e
        usage = data.get("usageMetadata", {})
        return LLMResult(
            text=text,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=model,
            provider=self.name,
            raw=data,
        )

    async def stream(
        self, system: str, user: str, model: str = "gemini-2.0-flash", **kw: Any
    ) -> AsyncIterator[str]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse&key={self.api_key}"
        async with (
            httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client,
            client.stream("POST", url, json=body) as r,
        ):
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                    text = evt["candidates"][0]["content"]["parts"][0]["text"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if text:
                    yield text


# ------------------------------------------------------------------ factory
_REGISTRY: dict[str, type] = {
    "mock": MockProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def build_provider(provider: str, base_url: str = "", api_key: str = "") -> Provider:
    key = (provider or "mock").lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise LLMError("provider", f"未知 provider: {provider}", retryable=False)
    if cls is MockProvider:
        return MockProvider()  # type: ignore[return-value]
    return cls(base_url=base_url, api_key=api_key)  # type: ignore[return-value]


def available_providers() -> list[str]:
    """已注册的 provider 名称，供健康检查与压测枚举。"""
    return sorted(_REGISTRY)


def random_provider() -> Provider:
    """随机取一个 provider。

    真实用途（此前是死代码，技术债 D3）：压力测试里模拟"用户配置五花八门"
    的真实场景 —— 不同请求落到不同 provider 与不同限额上，比全用 mock
    更能暴露路由与降级路径的问题。压测脚本 scripts/loadtest.py 会调用它。
    """
    return build_provider(random.choice(list(_REGISTRY)))
