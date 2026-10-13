"""题面导入解析（批次 C / F12）。

目标：把从洛谷 / Codeforces 复制来的题面文本（或 HTML 片段）解析成结构化数据，
再交给既有的两阶段难度标注落库。

设计取舍：
  * 不抓取网页 —— 由用户提供文本，避免爬虫的法律与封禁风险，也让功能离线可用；
  * 用"标签 + 正则"而不是引入 BeautifulSoup/lxml —— 题面结构简单，
    纯标准库足够，且不增加依赖面（lxml 虽已在环境里，但那是 Office 处理用的）；
  * 解析失败时明确报告"哪一段没认出来"，而不是静默产出空题面。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 段落识别
# 各平台的中英文段标题。顺序重要：先匹配更长的标题，避免"输入格式"被"输入"抢先。
SECTION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("statement", ("题目描述", "题目背景", "题面", "问题描述", "Problem", "Description")),
    ("input_desc", ("输入格式", "输入描述", "输入", "Input", "Input format")),
    ("output_desc", ("输出格式", "输出描述", "输出", "Output", "Output format")),
    ("constraints", ("数据范围", "数据规模与约定", "限制", "Constraints", "Limits")),
    ("hint", ("说明", "提示", "Hint", "Note", "Notes")),
    ("samples", ("样例", "输入输出样例", "Examples", "Example", "Sample", "Samples")),
]

# 难度关键词映射（中文平台常见标注）
DIFFICULTY_HINTS: list[tuple[str, str]] = [
    ("入门", "入门"),
    ("普及-", "普及-"),
    ("普及/提高-", "普及/提高-"),
    ("普及+/提高", "普及+/提高"),
    ("提高+/省选", "提高+/省选"),
    ("省选", "提高+/省选"),
    ("NOI", "提高+/省选"),
    ("IOI", "提高+/省选"),
    # Codeforces 分数段（粗略映射，最终以两阶段标注为准）
    ("800", "入门"),
    ("1000", "普及-"),
    ("1200", "普及-"),
    ("1400", "普及/提高-"),
    ("1600", "普及/提高-"),
    ("1800", "普及+/提高"),
    ("2000", "普及+/提高"),
    ("2400", "提高+/省选"),
]

# 知识点关键词 -> 标准标签名（与 seed 的标签体系对齐）
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "模拟": ("模拟", "simulation", "implement"),
    "枚举": ("枚举", "brute force", "bruteforce"),
    "排序": ("排序", "sort", "sorting"),
    "前缀和": ("前缀和", "prefix sum", "prefix"),
    "二分": ("二分", "binary search", "bisect"),
    "贪心": ("贪心", "greedy"),
    "双指针": ("双指针", "two pointers", "sliding window"),
    "搜索": ("搜索", "dfs", "bfs", "搜索", "depth-first", "breadth-first"),
    "动态规划": ("动态规划", "dp", "dynamic programming", "背包"),
    "图论": ("图论", "graph", "最短路", "shortest path", "dijkstra"),
    "并查集": ("并查集", "dsu", "union find", "union-find"),
    "线段树": ("线段树", "segment tree", "树状数组", "fenwick"),
    "数论": ("数论", "number theory", "gcd", "质数", "prime"),
    "字符串": ("字符串", "string", "kmp", "哈希"),
    "构造": ("构造", "constructive"),
    "计算几何": ("计算几何", "geometry", "凸包"),
}

# 算法深度：用于给规则引擎提供 algo_depth 特征
DEPTH_BY_TAG: dict[str, int] = {
    "模拟": 1,
    "枚举": 1,
    "排序": 2,
    "前缀和": 2,
    "二分": 3,
    "贪心": 3,
    "双指针": 3,
    "搜索": 3,
    "动态规划": 4,
    "图论": 4,
    "并查集": 4,
    "线段树": 5,
    "数论": 4,
    "字符串": 3,
    "构造": 3,
    "计算几何": 5,
}


@dataclass
class ParsedProblem:
    """解析结果。字段与 ProblemCreateIn 对齐，可直接落库。"""

    title: str = ""
    statement: str = ""
    input_desc: str = ""
    output_desc: str = ""
    constraints: str = ""
    samples: list[dict[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = "import"
    source_id: str = ""
    difficulty_hint: str = ""
    # 给难度规则引擎的特征
    statement_len: int = 0
    tag_count: int = 0
    algo_depth: int = 1
    constraint_tight: bool = False
    is_constructive: bool = False
    # 解析诊断：哪些段没识别出来
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "statement": self.statement,
            "input_desc": self.input_desc,
            "output_desc": self.output_desc,
            "constraints": self.constraints,
            "samples": self.samples,
            "tags": self.tags,
            "source": self.source,
            "source_id": self.source_id,
            "difficulty_hint": self.difficulty_hint,
            "statement_len": self.statement_len,
            "tag_count": self.tag_count,
            "algo_depth": self.algo_depth,
            "constraint_tight": self.constraint_tight,
            "is_constructive": self.is_constructive,
            "warnings": self.warnings,
        }


def strip_html(text: str) -> str:
    """把 HTML 片段转成纯文本（保留段落换行）。

    只处理题面里会出现的少数标签，不追求完整 HTML 解析。
    数学公式常见形式 $...$ 与 \\(...\\) 原样保留，前端用 KaTeX 渲染。

    注意：即使没有 "<"，也必须做实体解码 —— 有些题面从富文本编辑器复制
    出来只有 &lt; &amp; 这类实体而没有标签（踩过这个坑）。
    """
    if "<" in text:
        # 块级标签转换为换行，避免文字粘连
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        text = re.sub(r"(?i)<li[^>]*>", "· ", text)
        # 去掉其余标签
        text = re.sub(r"<[^>]+>", "", text)

    # 无论有没有标签，都要解码实体（&lt; &gt; &amp; &nbsp; 等）
    text = html.unescape(text)
    # 压缩过多空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_source(text: str) -> tuple[str, str]:
    """识别来源平台与题目编号。

    洛谷题号形如 P1001 / B2001 / CF1234A / AT_abc001_a，通常出现在标题行。
    """
    patterns = [
        (r"(?:www\.)?luogu\.(?:com\.cn|org)\.?/?problem/([A-Z]+\d+)", "luogu"),
        (r"洛谷\s*([A-Z]+\d+)", "luogu"),
        # CF URL 有两种形式：/contest/1234/problem/A 与 /problemset/problem/1234/A
        (
            r"codeforces\.com/(?:contest|problemset/problem)/(\d+)/(?:problem/)?([A-Z]\d?)",
            "codeforces",
        ),
        (r"\bCF\s*(\d+)\s*([A-Z]\d?)\b", "codeforces"),
        # 标题里的洛谷题号（P/B/T/U + 数字），只在前 200 字符里找，避免正文误命中
        (r"(?:^|\n)\s*([PBTU]\d{3,6})\b", "luogu"),
        # AtCoder
        (r"\b(AT_[a-z0-9_]+)\b", "atcoder"),
    ]
    head = text[:200]
    for pattern, source in patterns:
        m = re.search(pattern, head, re.I | re.M)
        if m:
            groups = [g for g in m.groups() if g]
            return source, "".join(groups)
    return "import", ""


def extract_title(lines: list[str]) -> str:
    """取标题：通常是第一行非空文本，去掉平台前缀。"""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        # 去掉 "P1001 A+B Problem" 这类编号前缀
        s = re.sub(r"^[A-Z]+\d+\s+", "", s)
        s = re.sub(r"^[A-Z]\d?\.\s*", "", s)
        return s[:200]
    return ""


def _match_section(line: str) -> str | None:
    """判断某行是否是段标题，返回段名。

    判定必须是**整行匹配**，不能用 startswith —— 踩过这个坑：
    "输入两个整数 a, b，输出它们的和。" 这行正文以"输入"开头，
    用 startswith 会被误判成"输入格式"段标题，导致题面正文整段丢失。

    同时排除样例标记行（"输入 #1"），它属于样例区而非段标题。
    """
    s = line.strip().strip("#*【】[]：:").strip()
    if not s or len(s) > 20:
        return None

    # 样例标记行（带编号）不算段标题
    if re.match(r"^(输入|输出|input|output)\s*(?:#|第)?\s*\d+\s*[:：]?$", s, re.I):
        return None

    lowered = s.lower()
    for name, titles in SECTION_PATTERNS:
        for t in titles:
            if lowered == t.lower():
                return name
            if lowered.startswith(t.lower()):
                # 只允许"标题 + 冒号/格式/描述"这类明确后缀。
                # 例如"输入格式"是标题，"输入两个整数"是正文 —— 必须区分开。
                rest = lowered[len(t.lower()) :].strip().strip("：:")
                if rest in ("", "格式", "描述", "说明", "与约定", "范围"):
                    return name
    return None


def parse_samples(text: str) -> list[dict[str, str]]:
    """解析样例。

    支持两种常见排版：
      1. "输入 #1" / "输出 #1" 交替出现；
      2. "输入:" 后紧跟内容，再 "输出:"。
    用状态机逐行扫描，避免正则跨段贪婪匹配。

    关键：标记行必须是**独立行** —— 只含"输入/输出 + 可选编号/冒号"。
    早期版本用宽松正则（允许标记后有任意文字），结果把"输入格式"这一
    段标题也当成了样例标记，导致状态机错乱、样例解析出垃圾数据。
    """
    samples: list[dict[str, str]] = []
    lines = text.split("\n")
    current_input: list[str] | None = None
    current_output: list[str] | None = None
    pending: str | None = None  # "input" | "output"

    # 独立标记行：输入/输出/Input/Output，可带 #1 / 1 / : / ：
    marker_re = re.compile(r"^\s*(输入|输出|input|output)\s*(?:#|第)?\s*(\d+)?\s*[:：]?\s*$", re.I)

    def flush() -> None:
        nonlocal current_input, current_output
        if current_input is None and current_output is None:
            return
        samples.append(
            {
                "input": "\n".join(current_input or []).strip(),
                "output": "\n".join(current_output or []).strip(),
            }
        )
        current_input = None
        current_output = None

    for line in lines:
        m = marker_re.match(line)
        if m:
            label = m.group(1).lower()
            kind = "input" if label in ("输入", "input") else "output"
            # 遇到新的"输入"标记且已收集过内容 -> 一段样例结束
            if kind == "input" and (current_input is not None or current_output is not None):
                flush()
            pending = kind
            if kind == "input":
                current_input = []
            else:
                current_output = []
            continue

        if pending == "input" and current_input is not None:
            current_input.append(line)
        elif pending == "output" and current_output is not None:
            current_output.append(line)

    flush()
    # 丢掉空样例
    return [s for s in samples if s["input"] or s["output"]]


def detect_tags(text: str) -> list[str]:
    """从题面里识别知识点标签（最多 4 个，按出现次数排序）。"""
    lowered = text.lower()
    scores: dict[str, int] = {}
    for tag, keywords in TAG_KEYWORDS.items():
        count = 0
        for kw in keywords:
            # 英文关键词按词边界匹配，避免 "dp" 命中 "adapt"
            if kw.isascii():
                count += len(re.findall(rf"\b{re.escape(kw)}\b", lowered))
            else:
                count += lowered.count(kw)
        if count:
            scores[tag] = count
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _ in ordered[:4]]


def detect_difficulty(text: str) -> str:
    """从题面里提取难度提示（仅作参考，最终以两阶段标注为准）。"""
    for hint, mapped in DIFFICULTY_HINTS:
        if hint in text:
            return mapped
    # Codeforces rating
    m = re.search(r"(?:rating|难度)\D{0,6}(\d{3,4})", text, re.I)
    if m:
        rating = int(m.group(1))
        if rating <= 900:
            return "入门"
        if rating <= 1200:
            return "普及-"
        if rating <= 1600:
            return "普及/提高-"
        if rating <= 2000:
            return "普及+/提高"
        return "提高+/省选"
    return ""


def parse_problem(raw: str, *, default_source: str = "import") -> ParsedProblem:
    """主入口：把题面文本解析成结构化数据。

    处理流程：
      1. 去 HTML、统一换行；
      2. 识别来源与编号；
      3. 按段标题切分题面/输入/输出/范围；
      4. 解析样例；
      5. 推断标签、难度提示与规则引擎特征。
    """
    result = ParsedProblem()
    if not raw or not raw.strip():
        result.warnings.append("输入为空")
        return result

    text = strip_html(raw.replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")

    result.source, result.source_id = detect_source(text)
    if result.source == "import" and default_source:
        result.source = default_source

    result.title = extract_title(lines)

    # ---- 按段标题切分 ----
    sections: dict[str, list[str]] = {}
    current = "statement"
    statement_started = False

    for idx, line in enumerate(lines):
        # 第一行若是标题则跳过（已单独提取）
        if idx == 0 and line.strip() == result.title:
            statement_started = True
            continue
        name = _match_section(line)
        if name is not None:
            current = name
            statement_started = True
            continue
        if not statement_started:
            continue
        sections.setdefault(current, []).append(line)

    result.statement = "\n".join(sections.get("statement", [])).strip()
    result.input_desc = "\n".join(sections.get("input_desc", [])).strip()
    result.output_desc = "\n".join(sections.get("output_desc", [])).strip()
    result.constraints = "\n".join(sections.get("constraints", [])).strip()

    # ---- 样例 ----
    sample_text = "\n".join(sections.get("samples", []))
    if not sample_text:
        # 有些题面把样例直接放在题面末尾，全文兜底解析
        sample_text = text
    result.samples = parse_samples(sample_text)
    if not result.samples:
        result.warnings.append("未识别到样例（不影响导入，可后续手工补充）")

    # ---- 标签与特征 ----
    full_text = f"{result.title}\n{result.statement}\n{result.constraints}\n{text}"
    result.tags = detect_tags(full_text)
    result.difficulty_hint = detect_difficulty(text)

    result.statement_len = len(result.statement)
    result.tag_count = max(1, len(result.tags))
    result.algo_depth = max([DEPTH_BY_TAG.get(t, 1) for t in result.tags] or [1])
    result.constraint_tight = bool(
        re.search(
            r"(10\s*\^\s*[5-9]|1e[5-9]|2\s*×\s*10\s*\^\s*5|10\^9)", result.constraints + text, re.I
        )
    )
    result.is_constructive = "构造" in full_text or "constructive" in full_text.lower()

    if not result.statement:
        result.warnings.append("未识别到题面正文，请检查格式")
    if not result.input_desc:
        result.warnings.append("未识别到输入格式")
    if not result.output_desc:
        result.warnings.append("未识别到输出格式")

    return result


def parse_many(raws: list[str], *, default_source: str = "import") -> list[ParsedProblem]:
    """批量解析（一次导入多道题）。"""
    return [parse_problem(r, default_source=default_source) for r in raws]
