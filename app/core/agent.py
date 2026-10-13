"""Agent 统一基类：结构化输出 + 校验 + 重试 + 规则引擎降级。

简历原话对应实现：
  "单 Agent 失败自动降级为规则引擎，主链路不中断"
      -> run() 捕获一切异常（校验失败/超时/限流/网络/未知），
         转而调用 run_rule()，返回 degraded=True 的 AgentResult，
         上层 pipeline 只看到"成功但降级"，链路继续。

  "统一基类 + 结构化输出(Pydantic 校验)"
      -> 子类只声明 schema / system_prompt / build_user() / run_rule()，
         序列化、重试、校验、统计、落库全部在基类完成。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import settings
from .cache import cache_key, get_cache, set_cache
from .providers import LLMError, LLMResult, Provider, _approx_tokens, build_provider

T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentContext:
    """Agent 执行上下文：所有输入数据都在这里，Agent 不直接碰数据库。"""

    user_id: str = ""
    provider: str = settings.DEFAULT_PROVIDER
    model: str = settings.DEFAULT_MODEL
    base_url: str = settings.DEFAULT_BASE_URL
    api_key: str = settings.DEFAULT_API_KEY
    temperature: float = 0.2
    task_id: str = ""
    use_cache: bool = True
    # 学习记忆清单（由 core.memory 预聚合，已是"结论"而非历史流水）。
    # 单独一个字段而不是塞进 data：它不参与业务逻辑，只参与 prompt 拼接，
    # 且需要显式控制是否注入（见 BaseAgent.inject_memory）。
    memory: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class AgentResult(Generic[T]):
    agent: str
    data: T | None
    degraded: bool = False
    confidence: float = 0.0
    reason: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    attempts: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def status(self) -> str:
        if self.data is None:
            return "failed"
        return "degraded" if self.degraded else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "degraded": self.degraded,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "tokens": self.prompt_tokens + self.completion_tokens,
            "cached": self.cached,
            "attempts": self.attempts,
            "data": self.data.model_dump() if self.data is not None else None,
        }


class BaseAgent(Generic[T]):
    """所有 Agent 的父类。子类需要覆盖：name / schema / system_prompt。

    可选覆盖：
        build_user(ctx)  -> 拼 prompt（默认走 ctx.data["prompt"]）
        run_rule(ctx)    -> 规则引擎兜底（默认抛 NotImplementedError -> 视为彻底失败）
        postprocess(data, ctx) -> 结果后处理（裁剪/约束/归一化）
        cacheable        -> 是否参与结果缓存
    """

    name: str = "base"
    schema: type[BaseModel] = BaseModel
    system_prompt: str = ""
    cacheable: bool = True
    max_retry: int = settings.LLM_MAX_RETRY
    timeout: float = settings.LLM_TIMEOUT

    # ------------------------------------------------------------- 对外入口
    async def run(self, ctx: AgentContext) -> AgentResult[T]:
        started = time.perf_counter()
        key = ""
        if self.cacheable and ctx.use_cache:
            key = cache_key(
                self.name,
                ctx.model,
                ctx.temperature,
                self.build_user(ctx),
                ctx.get("cache_salt", ""),
            )
            hit = get_cache(key)
            if hit is not None:
                try:
                    data = self.schema.model_validate(hit["data"])
                    return AgentResult(
                        agent=self.name,
                        data=data,
                        degraded=hit.get("degraded", False),
                        confidence=hit.get("confidence", 0.9),
                        provider=hit.get("provider", "cache"),
                        model=hit.get("model", ctx.model),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        cached=True,
                    )
                except ValidationError:
                    pass  # 缓存脏了就忽略，重新算

        provider = build_provider(ctx.provider, ctx.base_url, ctx.api_key)
        user_prompt = self.with_memory(self.build_user(ctx), ctx)
        last_error = ""
        last_kind = ""

        for attempt in range(1, self.max_retry + 2):  # 1 次首跑 + N 次重试
            try:
                result: LLMResult = await asyncio.wait_for(
                    provider.complete(
                        self.system_prompt,
                        user_prompt,
                        self.json_schema(),
                        ctx.model,
                        temperature=ctx.temperature,
                    ),
                    timeout=self.timeout,
                )
                data = self.parse(result.text)
                data = self.postprocess(data, ctx)
                if self.cacheable and ctx.use_cache and key:
                    set_cache(
                        key,
                        {
                            "data": data.model_dump(),
                            "degraded": False,
                            "confidence": self.confidence_of(data),
                            "provider": result.provider,
                            "model": result.model,
                        },
                        settings.CACHE_TTL_SECONDS,
                    )
                return AgentResult(
                    agent=self.name,
                    data=data,
                    degraded=False,
                    confidence=self.confidence_of(data),
                    provider=result.provider,
                    model=result.model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    attempts=attempt,
                )
            except TimeoutError:
                last_kind, last_error = "timeout", f"超时 {self.timeout}s"
            except ValidationError as e:
                last_kind, last_error = "bad_json", f"结构校验失败: {str(e)[:160]}"
            except LLMError as e:
                last_kind, last_error = e.kind, e.message[:160]
                if not e.retryable:
                    break
            except Exception as e:  # 兜底，绝不让异常冒到主链路
                last_kind, last_error = "unknown", f"{type(e).__name__}: {str(e)[:160]}"
            if attempt <= self.max_retry:
                await asyncio.sleep(min(0.4 * 2 ** (attempt - 1), 3.0))

        return await self._degrade(ctx, started, last_kind, last_error, provider)

    # ------------------------------------------------------------- 流式输出
    async def run_stream(self, ctx: AgentContext) -> AsyncIterator[dict[str, Any]]:
        """流式执行：逐 token 推原文，字段闭合即推结构化片段，最后推完整结果。

        事件类型：
          {"event": "start",   "agent": name}
          {"event": "token",   "text": "..."}          # 原始增量
          {"event": "field",   "key": "summary", "value": ...}  # 已闭合字段
          {"event": "done",    "result": {...}}        # 完整 AgentResult
          {"event": "error",   "reason": "..."}        # 失败且降级也失败

        容错原则与 run() 一致：任何异常都不向外抛，而是降级到规则引擎后
        以 done 事件收尾，保证前端永远能收到终态。
        """
        started = time.perf_counter()
        provider = build_provider(ctx.provider, ctx.base_url, ctx.api_key)
        user_prompt = self.with_memory(self.build_user(ctx), ctx)
        yield {"event": "start", "agent": self.name, "provider": provider.name, "model": ctx.model}

        parser = IncrementalJSON()
        chunks: list[str] = []
        last_error = ""
        last_kind = ""

        try:
            stream = provider.stream(
                self.system_prompt,
                user_prompt,
                ctx.model,
                schema=self.json_schema(),
                temperature=ctx.temperature,
            )
            async for piece in stream:
                if not piece:
                    continue
                chunks.append(piece)
                yield {"event": "token", "text": piece}
                for key, value in parser.feed(piece):
                    yield {"event": "field", "key": key, "value": value}
        except TimeoutError:
            last_kind, last_error = "timeout", f"流式超时 {self.timeout}s"
        except LLMError as e:
            last_kind, last_error = e.kind, e.message[:160]
        except Exception as e:
            last_kind, last_error = "unknown", f"{type(e).__name__}: {str(e)[:160]}"

        full = "".join(chunks)
        if full.strip():
            try:
                data = self.postprocess(self.parse(full), ctx)
                yield {
                    "event": "done",
                    "result": AgentResult(
                        agent=self.name,
                        data=data,
                        degraded=False,
                        confidence=self.confidence_of(data),
                        provider=provider.name,
                        model=ctx.model,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_tokens=_approx_tokens(self.system_prompt + user_prompt),
                        completion_tokens=_approx_tokens(full),
                    ).to_dict(),
                }
                return
            except ValidationError as e:
                last_kind, last_error = "bad_json", f"结构校验失败: {str(e)[:160]}"

        # 流式路径失败 -> 走统一降级（与 run() 同一套逻辑）
        result = await self._degrade(ctx, started, last_kind or "stream", last_error, provider)
        if result.ok:
            yield {"event": "degraded", "reason": result.reason}
        yield {"event": "done", "result": result.to_dict()}

    # --------------------------------------------------------- 规则引擎降级
    async def _degrade(
        self,
        ctx: AgentContext,
        started: float,
        kind: str,
        error: str,
        provider: Provider,
    ) -> AgentResult[T]:
        try:
            data = self.run_rule(ctx)
            if data is None:
                raise ValueError("规则引擎未产出结果")
            return AgentResult(
                agent=self.name,
                data=data,
                degraded=True,
                confidence=self.confidence_of(data) * 0.6,  # 降级结果置信度打折
                reason=f"{kind}: {error}",
                provider=provider.name,
                model="rule-engine",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=error,
            )
        except Exception as e:
            return AgentResult(
                agent=self.name,
                data=None,
                degraded=True,
                confidence=0.0,
                reason=f"{kind}: {error}",
                provider=provider.name,
                model="none",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"降级也失败: {type(e).__name__}: {e}",
            )

    # --------------------------------------------------------------- 可覆盖
    def build_user(self, ctx: AgentContext) -> str:
        return str(ctx.get("prompt", ""))

    # 默认不注入记忆：只有"决策类"Agent 才需要知道用户的历史状态。
    # 判题归因看的是代码与用例，难度标注看的是题面 —— 给它们塞用户记忆
    # 只会增加 token 与干扰。子类显式声明 True 才注入。
    inject_memory: bool = False

    def with_memory(self, prompt: str, ctx: AgentContext) -> str:
        """把记忆清单拼到 prompt 前面（若该 Agent 需要且记忆非空）。

        放在 prompt **前面**而不是后面：记忆是"背景设定"，先给出背景再给
        具体任务，模型的遵从度更高；放在末尾容易被当成附注而忽略。
        """
        if not self.inject_memory or not ctx.memory:
            return prompt
        return f"{ctx.memory}\n\n{prompt}"

    def json_schema(self) -> dict[str, Any]:
        schema = self.schema.model_json_schema()
        schema.setdefault("title", self.schema.__name__)
        return schema

    def parse(self, text: str) -> T:
        """容错解析：直接 JSON -> 剥离 ```json 代码块 -> 截取首个 {..}。"""
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return self.schema.model_validate_json(text)
        except ValidationError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return self.schema.model_validate_json(text[start : end + 1])
            raise

    def postprocess(self, data: T, ctx: AgentContext) -> T:
        return data

    def confidence_of(self, data: T) -> float:
        value = getattr(data, "confidence", None)
        return float(value) if isinstance(value, (int, float)) else 0.9

    def run_rule(self, ctx: AgentContext) -> T | None:
        """默认无规则实现，返回 None 表示该 Agent 无降级能力。"""
        return None


# --------------------------------------------------------------- 结构化解析
class IncrementalJSON:
    """流式增量 JSON 解析器：token 流里逐字段吐出已闭合的顶层字段。

    用途（技术债 D2 的落地）：让"流式返回"与"结构化输出"同时成立 ——
      * 原始 token 直接推给前端，首字节延迟低；
      * 某个字段一旦闭合就立刻推 `field` 事件，前端可以先把已完成的字段渲染出来，
        而不是等整个 JSON 生成完。

    实现要点：用 scanner 逐个扫描顶层表达式并记录结束位置，
    而不是对缓冲区做正则搜索 —— 后者在"字段值里恰好含有同名字符串"时会错位。
    """

    def __init__(self, max_buffer: int = 512 * 1024) -> None:
        self.buf = ""
        self.emitted: set[str] = set()
        self.max_buffer = max_buffer
        self._truncated = False

    def feed(self, chunk: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        if not chunk:
            return out
        if self._truncated:
            return out
        self.buf += chunk
        if len(self.buf) > self.max_buffer:
            # 防止超长输出把内存吃光；截断后不再解析新字段
            self._truncated = True
            return out
        for key, value in self._scan_pairs():
            if key in self.emitted:
                continue
            self.emitted.add(key)
            out.append((key, value))
        return out

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _scan_pairs(self) -> list[tuple[str, Any]]:
        """扫描缓冲区内所有"已完成"的顶层 `"key": value` 对。

        用 JSONDecoder.raw_decode 在当前位置解析，它只在遇到完整合法的 JSON
        值时成功，因此天然表达"该字段是否已闭合"，且不会被值内的同名文本干扰。
        """
        pairs: list[tuple[str, Any]] = []
        decoder = json.JSONDecoder()
        i, n = 0, len(self.buf)
        while i < n:
            # 找下一个字符串的起始引号
            if self.buf[i] != '"':
                i += 1
                continue
            try:
                key, end = decoder.raw_decode(self.buf, i)
            except json.JSONDecodeError:
                # 这个引号可能是值内部的，跳过它继续找
                i += 1
                continue
            if not isinstance(key, str):
                i = max(end, i + 1)
                continue
            j = end
            while j < n and self.buf[j] in " \t\r\n":
                j += 1
            if j >= n or self.buf[j] != ":":
                # 不是 "key": 结构，说明这是某个字符串值，跳过
                i = max(end, i + 1)
                continue
            j += 1
            while j < n and self.buf[j] in " \t\r\n":
                j += 1
            if j >= n:
                break  # 值还没开始，等更多 token
            try:
                value, vend = decoder.raw_decode(self.buf, j)
            except json.JSONDecodeError:
                break  # 值未闭合，等更多 token

            # 数字需要"后一个字符是分隔符"才算真正结束：
            # 否则逐字符喂入时 "1.5" 会在读到 "1" 时就被判为已闭合，推出错误的值 1。
            is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
            if is_number and (vend >= n or self.buf[vend] not in ",}] \t\r\n"):
                break  # 数字后面还有字符，说明没读完
            pairs.append((key, value))
            i = vend
        return pairs


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()
