"""公共请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return (self.total + self.size - 1) // self.size


class Ok(BaseModel):
    ok: bool = True
    message: str = ""


class ErrorOut(BaseModel):
    detail: str


# ------------------------------------------------------------------ 认证
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_\-.]+$")
    # 长度下限 8：与 security.password_strength 的规则保持一致
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(default="", max_length=32)
    level: int = Field(default=3, ge=1, le=5)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class LoginIn(BaseModel):
    username: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10)


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=32)
    level: int | None = Field(default=None, ge=1, le=5)
    daily_goal: int | None = Field(default=None, ge=1, le=50)
    timezone: str | None = Field(default=None, max_length=64)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int = 0
    user_id: str
    username: str
    role: str = "user"


class UserOut(BaseModel):
    id: str
    username: str
    nickname: str
    level: int
    daily_goal: int
    role: str = "user"
    timezone: str = "Asia/Shanghai"
    created_at: datetime


# ------------------------------------------------------------ 模型配置
class ModelConfigIn(BaseModel):
    provider: str = Field(default="mock", pattern="^(mock|openai|anthropic|gemini)$")
    model: str = "mock-1"
    base_url: str = ""
    api_key: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=64, le=32000)
    purpose: str = "default"
    enabled: bool = True


class ModelConfigOut(BaseModel):
    provider: str
    model: str
    base_url: str
    api_key_masked: str
    temperature: float
    max_tokens: int
    purpose: str
    enabled: bool
    is_default: bool = False


# ------------------------------------------------------------------ 题库
class ProblemOut(BaseModel):
    id: str
    title: str
    difficulty: str
    difficulty_score: float
    difficulty_source: str
    tags: list[str] = []
    source: str
    time_limit_ms: int
    memory_limit_mb: int
    accepted_count: int
    submit_count: int


class ProblemDetail(ProblemOut):
    statement: str
    input_desc: str
    output_desc: str
    samples: list[Any] = []
    constraints: str


class ProblemCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    statement: str = ""
    input_desc: str = ""
    output_desc: str = ""
    samples: list[Any] = []
    constraints: str = ""
    tags: list[str] = []
    source: str = "local"
    source_id: str = ""
    difficulty: str = "入门"
    difficulty_score: float = 0.0
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    auto_annotate: bool = True


class TagOut(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    category: str
    description: str = ""
    prereq_ids: list[str] = []


# -------------------------------------------------------------- 提交评测
class SubmissionIn(BaseModel):
    problem_id: str
    language: str = "python"
    code: str = Field(min_length=1)
    # 判题模式：sandbox=真实沙箱执行；caller=沿用传入 verdict；auto=有用例走沙箱
    judge_mode: Literal["sandbox", "caller", "auto"] = "auto"
    # 仅 judge_mode=caller（或该题无用例）时才使用下列字段
    verdict: str = "WA"
    passed_cases: int = 0
    total_cases: int = 0
    runtime_ms: int = 0
    memory_kb: int = 0
    failed_case: str = ""
    judge_detail: dict[str, Any] = {}
    run_agents: bool = True
    # 窗口期内同题同代码去重（清单 N2），避免重复烧模型额度
    dedupe: bool = True


class SubmissionOut(BaseModel):
    id: str
    problem_id: str
    language: str
    verdict: str
    passed_cases: int
    total_cases: int
    runtime_ms: int
    memory_kb: int
    judged_by: str = "caller"
    created_at: datetime


class TestCaseIn(BaseModel):
    input: str = ""
    output: str = ""
    hidden: bool = True
    score: int = 10
    note: str = ""


class TestCaseOut(BaseModel):
    id: str
    order_no: int
    hidden: bool
    score: int
    note: str = ""
    # 隐藏用例不回显内容，防止用户据此伪造输出
    input_data: str = ""
    expected_output: str = ""


class TestCaseBatchIn(BaseModel):
    cases: list[TestCaseIn] = []
    replace: bool = True


class JudgeOut(BaseModel):
    submission_id: str
    verdict: str
    root_cause: str = ""
    explanation: str = ""
    fix_suggestion: str = ""
    degraded: bool = False
    confidence: float = 0.0


# ------------------------------------------------------------ 推荐/路线图
class RecommendQuery(BaseModel):
    target_score: float = 55.0
    limit: int = 20
    exclude_recent: int = 20


class RecommendResult(BaseModel):
    problem_id: str
    title: str
    difficulty: str
    tags: list[str]
    reason: str
    expected_gain: float
    degraded: bool
    provider: str


class RoadmapQuery(BaseModel):
    weeks: int = 6
    target_level: str = "L4"
    daily_hours: float = 2.0


class RoadmapResult(BaseModel):
    stages: list[dict[str, Any]]
    rationale: str
    degraded: bool


# ------------------------------------------------------------------ 复习
class ReviewMarkIn(BaseModel):
    problem_id: str
    rating: int = Field(default=3, ge=1, le=4, description="1=again 2=hard 3=good 4=easy")
    verdict: str = "AC"


# ------------------------------------------------------------------ 掌握度
class MasteryOut(BaseModel):
    score: float
    level: str
    confidence: float
    weak_tags: list[str]
    strong_tags: list[str]
    suggestion: str = ""
    metrics: dict[str, Any] = {}


class MasteryProfileOut(BaseModel):
    tag_id: str
    tag: str
    score: float
    confidence: float
    attempts: int
    ac_count: int
    level: str


# ------------------------------------------------------------ Agent/任务
class AgentRunIn(BaseModel):
    agent: str
    payload: dict[str, Any] = {}
    use_cache: bool = True


class AgentRunOut(BaseModel):
    agent: str
    status: str
    degraded: bool
    confidence: float
    provider: str
    model: str
    latency_ms: int
    tokens: int
    cached: bool
    data: Any = None


class PipelineIn(BaseModel):
    submission: SubmissionIn
    include_roadmap: bool = False
    weeks: int = 6


class TaskOut(BaseModel):
    task_id: str
    status: str
    progress: float
    stage: str
    error: str = ""
    result: dict[str, Any] = {}
    elapsed_ms: int
