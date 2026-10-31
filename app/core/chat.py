"""对话式辅导引擎：多轮陪跑 + 强度治理 + 规则降级。

与单发 hint（agents/hint.py）的分工：
  * hint  = "一次要一个提示"——结构化输出、走 Agent 注册表；
  * chat  = "陪跑到想明白"——自由文本多轮对话，这里独立成模块而不塞进
    BaseAgent 注册表：结构化 Agent 与对话是两种形态（schema 校验 vs
    流式自由文本），硬套同一基类只会互相将就。

强度治理（延续"策略归规则、表达归模型"的哲学）：
  * 初始强度 = hint_strength_for(该题最弱标签掌握度, 提交次数)；
  * 用户明确要更多（"再点拨""不懂""更直接"）才升一档，封顶 pseudocode；
  * 用户表示想通了 → 降回 direction（避免后续轮次继续灌输）；
  * **任何档位都不得输出可提交的完整代码**——训练价值不能归零。

上下文预算（token 可控，与 core.memory 同一设计取向）：
  题面 ≤1200 字 + 记忆块（结论非历史）+ 最近 10 轮（每轮 ≤600 字）+ 本轮 ≤2000 字。

降级链：真模型失败（超时/坏输出/网络）→ rule_reply() 模板化辅导，
degraded=True 照常入库 —— 主链路永不中断，与 BaseAgent._degrade 同语义。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .rules import hint_strength_for

STRENGTH_ORDER = ["direction", "key_observation", "pseudocode"]

# 轮次裁剪预算
MAX_HISTORY_TURNS = 10
MAX_TURN_CHARS = 600
MAX_USER_CHARS = 2000
MAX_STATEMENT_CHARS = 1200

SYSTEM = """你是算法刷题教练，正在和学生进行「多轮对话式辅导」。你的目标不是给出答案，
而是陪他把这道题自己想明白。

【最重要的一条】你**绝对不能**给出可直接提交的完整代码或完整算法步骤。
学生要自己写出来，你的价值是让他"想到"，不是让他"抄到"。

当前提示强度档位（系统按学生掌握度算定，你只能在该档位内表达，不得自行升降）：
  * direction（弱）：只点方向 —— 往哪类算法/性质上想；
  * key_observation（中）：点出关键观察与需要检查的边界，但不给步骤；
  * pseudocode（强）：自然语言描述 3 步以内的骨架（**不写真实代码**）。

对话原则：
1. 回答简短聚焦（通常 3-6 句），一次只推进一步 —— 多轮的价值就是小步走；
2. 学生说"我想到了 X"时，先判断 X 对不对：对就肯定并推下一步，错就指出
   错在哪一层（理解/方向/边界），不要直接给正确答案；
3. 学生要"再提示/更直接"时，可以在当前档位内说得更具体，但不要跳档；
4. 学生明显已经想通时，鼓励他自己写代码并跑样例，不要再灌输；
5. 用中文回答。"""


@dataclass
class ChatTurn:
    role: str  # user | assistant
    content: str
    strength: str = ""


@dataclass
class CoachContext:
    """一次辅导调用的全部输入（API 层组装，引擎不碰数据库）。"""

    problem: dict[str, Any] = field(default_factory=dict)
    memory: str = ""
    mastery_score: float = 50.0
    attempts: int = 0
    history: list[ChatTurn] = field(default_factory=list)
    # 调用方自定义的附加背景块（如面试官需要的"最近提交结果"），独立 system 块注入
    extra: str = ""
    # 本次调用的 LLM 超时覆盖（秒）：0 = 用全局 LLM_TIMEOUT
    timeout: float = 0


@dataclass
class CoachReply:
    text: str
    strength: str
    degraded: bool = False
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ------------------------------------------------------------- 强度治理
_ESCALATE_PAT = re.compile(
    r"再.{0,6}(提示|点拨|直接|明白|讲)|还是不懂|没看懂|不理解|更直接|到底怎么"
)
_SETTLED_PAT = re.compile(r"我想到了|我懂了|明白了|想通了|我知道了|我来写|我自己写")


def next_strength(current: str, user_message: str) -> str:
    """按用户表态升降档：要更多 → 升一档（封顶 pseudocode）；想通了 → 回最弱档。

    刻意做成纯函数：治理规则必须可单测、可复现，模型与前端都只能感知结果。
    """
    idx = STRENGTH_ORDER.index(current) if current in STRENGTH_ORDER else 1
    if _SETTLED_PAT.search(user_message or ""):
        return STRENGTH_ORDER[0]
    if _ESCALATE_PAT.search(user_message or ""):
        return STRENGTH_ORDER[min(idx + 1, len(STRENGTH_ORDER) - 1)]
    return STRENGTH_ORDER[idx]


def initial_strength(mastery_score: float, attempts: int) -> str:
    """会话初始强度：与单发 hint 完全同一口径（rules.hint_strength_for）。"""
    strength, _reason = hint_strength_for(mastery_score, attempts)
    return strength


# ------------------------------------------------------------- 上下文组装
def build_messages(ctx: CoachContext, user_message: str, strength: str) -> list[dict[str, str]]:
    """组装多轮消息：题面 + 记忆（结论）+ 近 N 轮 + 本轮。

    裁剪都发生在"旧轮次"与"超长输入"上：最近的、本轮的信息优先保真。
    """
    blocks: list[str] = []

    if ctx.problem:
        tags = "、".join(str(t) for t in (ctx.problem.get("tags") or []))
        statement = str(ctx.problem.get("statement", ""))[:MAX_STATEMENT_CHARS]
        blocks.append(
            "【当前题目】\n"
            f"标题：{ctx.problem.get('title', '')}\n"
            f"知识点：{tags or '未标注'}　难度：{ctx.problem.get('difficulty', '')}\n"
            f"题面：{statement}"
        )
    if ctx.memory:
        blocks.append(f"【学生学习画像（聚合结论，非历史流水）】\n{ctx.memory}")

    turns = ctx.history[-MAX_HISTORY_TURNS:]
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM + f"\n\n当前档位：{strength}"}
    ]
    if blocks:
        messages.append({"role": "system", "content": "\n\n".join(blocks)})
    for t in turns:
        content = (t.content or "")[:MAX_TURN_CHARS]
        messages.append({"role": "user" if t.role == "user" else "assistant", "content": content})
    messages.append({"role": "user", "content": (user_message or "")[:MAX_USER_CHARS]})
    return messages


# ------------------------------------------------------------- 规则降级
def rule_reply(ctx: CoachContext, user_message: str, strength: str) -> str:
    """模板化辅导（离线/mock/降级共用）：不知道题目的具体算法就不假装知道。

    与 rule_hint 同一诚实原则：给"该强度下的思考框架"与下一步动作，
    并说明这是规则引擎回复。
    """
    tags = [str(t) for t in (ctx.problem.get("tags") or [])]
    tag_text = "、".join(tags[:3]) if tags else "本题知识点"
    asked_more = bool(_ESCALATE_PAT.search(user_message or ""))
    settled = bool(_SETTLED_PAT.search(user_message or ""))

    if settled:
        return (
            "很好，方向已经对了。现在把它写成代码：先处理输入，再按你刚才说的思路走主循环，"
            "最后跑一遍样例和边界（空输入/单元素/极端值）。有结果了随时回来对答案。"
        )
    pool = {
        "direction": (
            f"（规则引擎回复）先把题面的输入输出和数据范围抄一遍，判断{tag_text}里哪一步是瓶颈；"
            "再用小样例手算一遍，观察答案是怎么一步步得到的。"
        ),
        "key_observation": (
            f"（规则引擎回复）{tag_text}类问题通常有一个「单调性」或「可合并」的性质——"
            "先把它找出来；同时把边界条件单独列出来逐条确认（空输入、单元素、包含关系）。"
        ),
        "pseudocode": (
            "（规则引擎回复）三步骨架：①读入并按所需结构整理数据；"
            "②主循环中维护当前状态并判断何时更新答案；③输出并用样例与边界各验一遍。"
        ),
    }
    reply = pool[strength if strength in pool else "key_observation"]
    if asked_more:
        reply += "（你要求更直接——建议带上你当前的代码再来问，能定位到具体卡点。）"
    return reply


# ------------------------------------------------------------- 主入口
async def coach_reply(
    config: dict[str, Any], ctx: CoachContext, user_message: str, strength: str
) -> CoachReply:
    """非流式回复（测试与非流式客户端用）；失败降级 rule_reply。"""
    if str(config.get("provider", "mock")) == "mock":
        # mock = 无信息模型：对话场景下直接给规则模板（比 schema 驱动的
        # 假 JSON 更诚实），离线演示与降级路径共用同一实现
        return CoachReply(text=rule_reply(ctx, user_message, strength), strength=strength)

    from .providers import LLMError, build_provider

    provider = build_provider(
        str(config.get("provider", "")),
        str(config.get("base_url", "")),
        str(config.get("api_key", "")),
    )
    messages = build_messages(ctx, user_message, strength)
    # ctx.timeout 优先（概览页等在线场景传短超时快速降级），0 用全局默认
    effective_timeout = ctx.timeout or float(config.get("timeout", 0) or 0) or settings.LLM_TIMEOUT
    try:
        result = await asyncio.wait_for(
            provider.complete_chat(
                messages,
                str(config.get("model", "")),
                temperature=float(config.get("temperature", 0.3)),
            ),
            timeout=effective_timeout,
        )
        text = (result.text or "").strip()
        if not text:
            raise ValueError("空回复")
        return CoachReply(
            text=text,
            strength=strength,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    except (TimeoutError, LLMError, ValueError, Exception) as e:
        return CoachReply(
            text=rule_reply(ctx, user_message, strength),
            strength=strength,
            degraded=True,
            reason=f"{type(e).__name__}: {str(e)[:120]}",
        )


async def coach_reply_stream(
    config: dict[str, Any], ctx: CoachContext, user_message: str, strength: str
):
    """流式回复：逐 token 产出文本片段，收尾产出 CoachReply。

    失败时降级为一次性产出 rule_reply（前端拿到完整终态，与
    BaseAgent.run_stream 的"永远有终态"契约一致）。
    """
    if str(config.get("provider", "mock")) == "mock":
        reply = CoachReply(text=rule_reply(ctx, user_message, strength), strength=strength)
        for i in range(0, len(reply.text), 24):
            await asyncio.sleep(0.01)
            yield reply.text[i : i + 24]
        yield reply
        return

    from .providers import LLMError, build_provider

    provider = build_provider(
        str(config.get("provider", "")),
        str(config.get("base_url", "")),
        str(config.get("api_key", "")),
    )
    messages = build_messages(ctx, user_message, strength)
    chunks: list[str] = []
    try:
        stream = provider.stream_chat(
            messages,
            str(config.get("model", "")),
            temperature=float(config.get("temperature", 0.3)),
        )
        async for piece in stream:
            if piece:
                chunks.append(piece)
                yield piece
        text = "".join(chunks).strip()
        if not text:
            raise ValueError("空回复")
        yield CoachReply(text=text, strength=strength)
    except (TimeoutError, LLMError, ValueError, Exception) as e:
        # 流中途失败：已推的 token 无法收回，降级文本整体补发
        yield CoachReply(
            text=rule_reply(ctx, user_message, strength),
            strength=strength,
            degraded=True,
            reason=f"{type(e).__name__}: {str(e)[:120]}",
        )


__all__ = [
    "STRENGTH_ORDER",
    "ChatTurn",
    "CoachContext",
    "CoachReply",
    "build_messages",
    "coach_reply",
    "coach_reply_stream",
    "initial_strength",
    "next_strength",
    "rule_reply",
]
