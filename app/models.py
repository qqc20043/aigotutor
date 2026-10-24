"""14 张核心表。SQLAlchemy 2.0 声明式写法，全部使用跨库通用类型，方便切 MySQL。

表清单：
  1  users                 用户
  2  user_model_configs    用户级模型配置
  3  problems              题库
  4  tags                  知识点标签（树形）
  5  problem_tags          题目-标签多对多 + 权重
  6  submissions           提交记录
  7  reviews               题解与评测结果（Agent 产出）
  8  mastery_profiles      掌握度画像
  9  review_queue          间隔重复队列（FSRS 简化版）
 10  wrong_book            错题本
 11  error_clusters        易错题聚类
 12  learning_states       0-100 状态快照（五级 + 时间序列）
 13  agent_runs            Agent 执行记录（含降级标记、token 消耗）
 14  background_tasks      后台任务（状态/进度/可中断）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """UTC 语义的 DateTime：库里存 naive UTC 字符串，读出来是带时区的 aware。

    为什么需要它（真实缺陷）：SQLite 的 DateTime 列不带时区，写进去的 aware
    datetime 被存成裸字符串；读回来时 tzinfo 丢失，API 于是输出
    "2026-10-13T09:33:40"（无 Z 后缀）。前端 `new Date()` 会把这种字符串按
    **本地时间**解析 —— 东八区用户看到"刚刚提交"显示成"8 小时前"，全站时间
    统一偏 8 小时。

    修在这里而不是前端：时间戳语义属于数据层，任何 API 消费者（前端、脚本、
    第三方）都应拿到无歧义的 UTC。存储格式不变（仍是 naive UTC），因此
    无需数据迁移。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------- 1 users
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[int] = mapped_column(Integer, default=1)  # 用户自评水平 1-5
    daily_goal: Mapped[int] = mapped_column(Integer, default=3)  # 每日刷题目标
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- 认证增强（批次 C） ---
    # 角色：user 普通用户 / admin 管理员。管理接口按此鉴权。
    role: Mapped[str] = mapped_column(String(16), default="user")
    # token 版本：改密、登出全部设备时 +1，旧 token 立即失效。
    # 相比"黑名单"方案，无需额外存储与清理，且天然支持"踢下线"。
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # 用户时区（IANA 名，如 Asia/Shanghai）：打卡日界、日榜按此计算
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    configs: Mapped[list[UserModelConfig]] = relationship(
        back_populates="user", passive_deletes=True
    )
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="user", passive_deletes=True
    )


# ------------------------------------------------- 2 user_model_configs
class UserModelConfig(Base, TimestampMixin):
    """用户级模型配置：同一部署下每个用户可用不同 provider / key / model。"""

    __tablename__ = "user_model_configs"
    __table_args__ = (UniqueConstraint("user_id", "purpose", name="uq_user_purpose"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32), default="default")  # default|fast|reason
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    model: Mapped[str] = mapped_column(String(64), default="mock-1")
    base_url: Mapped[str] = mapped_column(String(200), default="")
    api_key_ref: Mapped[str] = mapped_column(String(200), default="")  # 只存引用/密文
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="configs")


# ------------------------------------------------------------ 3 problems
class Problem(Base, TimestampMixin):
    __tablename__ = "problems"
    __table_args__ = (
        Index("ix_problems_difficulty", "difficulty"),
        Index("ix_problems_source", "source", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    statement: Mapped[str] = mapped_column(Text, default="")  # 题面
    input_desc: Mapped[str] = mapped_column(Text, default="")
    output_desc: Mapped[str] = mapped_column(Text, default="")
    samples: Mapped[str] = mapped_column(Text, default="[]")  # JSON: 样例
    constraints: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="local")  # local|luogu|cf
    source_id: Mapped[str] = mapped_column(String(64), default="")
    # 创建者（空 = 导入/种子数据，仅管理员可改其用例）。此前任何登录用户
    # 都能覆写任意题目的用例（含隐藏期望输出）—— 等于能伪造判题结果。
    created_by: Mapped[str] = mapped_column(String(32), default="", index=True)
    difficulty: Mapped[str] = mapped_column(String(16), default="入门")
    difficulty_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100 连续分
    difficulty_source: Mapped[str] = mapped_column(String(16), default="rule")  # rule|llm|human
    time_limit_ms: Mapped[int] = mapped_column(Integer, default=1000)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=256)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    submit_count: Mapped[int] = mapped_column(Integer, default=0)

    # passive_deletes=True：删除题目时把级联交给数据库外键（ON DELETE CASCADE），
    # 而不是让 ORM 逐行处理子记录。
    #
    # 不设这个参数会怎样（实测踩到的 500）：ORM 默认会把子行的外键置 NULL，
    # 而 problem_tags.problem_id 是 NOT NULL，于是删除题目直接抛
    # "NOT NULL constraint failed: problem_tags.problem_id"。
    # 同理适用于下面所有带 CASCADE 外键的父子关系。
    tags: Mapped[list[ProblemTag]] = relationship(back_populates="problem", passive_deletes=True)


# ---------------------------------------------------------------- 4 tags
class Tag(Base, TimestampMixin):
    """知识点标签，树形结构：parent_id 为空即根节点。"""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", name="uq_tag_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("tags.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(32), default="algorithm")
    description: Mapped[str] = mapped_column(Text, default="")
    # 该知识点的先修标签 id 列表（JSON），供路线图拓扑排序使用
    prereq_ids: Mapped[str] = mapped_column(Text, default="[]")


# -------------------------------------------------------- 5 problem_tags
class ProblemTag(Base):
    __tablename__ = "problem_tags"
    __table_args__ = (
        UniqueConstraint("problem_id", "tag_id", name="uq_problem_tag"),
        Index("ix_problem_tags_tag", "tag_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"))
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # 主标签 1.0，次标签 0.5
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    problem: Mapped[Problem] = relationship(back_populates="tags")
    tag: Mapped[Tag] = relationship()


# ---------------------------------------------------- 5b testcases（第 15 张表）
class TestCase(Base, TimestampMixin):
    """题目测试用例（判题沙箱的数据源）。

    hidden=True 的用例只参与判题，不下发给用户 —— 这是竞赛平台的基本要求：
    否则用户可以直接打印期望输出骗过评测。
    """

    __tablename__ = "testcases"
    __table_args__ = (
        Index("ix_testcases_problem", "problem_id", "order_no"),
        UniqueConstraint("problem_id", "order_no", name="uq_testcase_order"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    order_no: Mapped[int] = mapped_column(Integer, default=1)
    input_data: Mapped[str] = mapped_column(Text, default="")
    expected_output: Mapped[str] = mapped_column(Text, default="")
    hidden: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[int] = mapped_column(Integer, default=10)
    note: Mapped[str] = mapped_column(String(200), default="")


# ------------------------------------------------------- 6 submissions
class Submission(Base, TimestampMixin):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_user_time", "user_id", "created_at"),
        Index("ix_submissions_problem", "problem_id"),
        Index("ix_submissions_idem", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    language: Mapped[str] = mapped_column(String(16), default="python")
    code: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(
        String(16), default="PENDING"
    )  # AC|WA|TLE|MLE|RE|CE|OLE|IE|PENDING
    passed_cases: Mapped[int] = mapped_column(Integer, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    runtime_ms: Mapped[int] = mapped_column(Integer, default=0)
    memory_kb: Mapped[int] = mapped_column(Integer, default=0)
    failed_case: Mapped[str] = mapped_column(Text, default="")
    judge_detail: Mapped[str] = mapped_column(Text, default="{}")
    # 幂等键（清单 N3）：前端重试/网络抖动不应产生重复提交记录
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 代码指纹：用于同用户同题短时去重（清单 N2），避免重复烧 token
    code_hash: Mapped[str] = mapped_column(String(32), default="")
    # 是否由真实沙箱判题（区别于调用方传入 verdict）
    judged_by: Mapped[str] = mapped_column(String(16), default="caller")  # sandbox|caller

    user: Mapped[User] = relationship(back_populates="submissions")
    # passive_deletes：删除提交时由数据库级联删评审（理由见 Problem.tags 注释）
    reviews: Mapped[list[Review]] = relationship(back_populates="submission", passive_deletes=True)


# ----------------------------------------------------------- 7 reviews
class Review(Base, TimestampMixin):
    """题解与评测结果：Agent 产出，可含多份（题解/复杂度/易错点）。"""

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_submission", "submission_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24), default="solution")  # solution|complexity|pitfall
    content: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[str] = mapped_column(Text, default="{}")  # JSON 结构化结果
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    submission: Mapped[Submission] = relationship(back_populates="reviews")


# -------------------------------------------------- 8 mastery_profiles
class MasteryProfile(Base, TimestampMixin):
    """掌握度画像：user × tag 维度的分数与置信度。"""

    __tablename__ = "mastery_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "tag_id", name="uq_mastery_user_tag"),
        Index("ix_mastery_user_score", "user_id", "score"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0-1
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ac_count: Mapped[int] = mapped_column(Integer, default=0)
    last_practiced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ------------------------------------------------------- 9 review_queue
class ReviewQueue(Base, TimestampMixin):
    """间隔重复队列（FSRS 简化版：稳定性 + 难度 + 可提取性）。"""

    __tablename__ = "review_queue"
    __table_args__ = (
        UniqueConstraint("user_id", "problem_id", name="uq_review_user_problem"),
        Index("ix_review_due", "user_id", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    stability: Mapped[float] = mapped_column(Float, default=1.0)  # FSRS S：记忆稳定度（天）
    difficulty_fsrs: Mapped[float] = mapped_column(Float, default=5.0)  # FSRS D：1-10
    retrievability: Mapped[float] = mapped_column(Float, default=1.0)  # R：可提取性
    interval_days: Mapped[float] = mapped_column(Float, default=1.0)
    reps: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_review_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ---------------------------------------------------------- 10 wrong_book
class WrongBook(Base, TimestampMixin):
    __tablename__ = "wrong_book"
    __table_args__ = (UniqueConstraint("user_id", "problem_id", name="uq_wrong_user_problem"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    wrong_count: Mapped[int] = mapped_column(Integer, default=1)
    last_verdict: Mapped[str] = mapped_column(String(16), default="WA")
    last_wrong_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    root_cause: Mapped[str] = mapped_column(String(64), default="")  # 归因：边界/复杂度/理解偏差
    note: Mapped[str] = mapped_column(Text, default="")
    mastered: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------ 11 error_clusters
class ErrorCluster(Base, TimestampMixin):
    """易错题聚类：同一错误模式的题目归到一簇。"""

    __tablename__ = "error_clusters"
    __table_args__ = (Index("ix_cluster_user", "user_id", "cluster_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    cluster_key: Mapped[str] = mapped_column(String(64), nullable=False)  # 标签组合指纹
    label: Mapped[str] = mapped_column(String(64), default="")
    problem_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    tag_ids: Mapped[str] = mapped_column(Text, default="[]")
    size: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[float] = mapped_column(Float, default=0.0)  # 错题数 × 权重
    suggestion: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------- 12 learning_states
class LearningState(Base, TimestampMixin):
    """0-100 学习状态快照：五级分档 + 时间序列。"""

    __tablename__ = "learning_states"
    __table_args__ = (Index("ix_state_user_time", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    level: Mapped[str] = mapped_column(String(8), default="L1")  # L1-L5
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    metrics: Mapped[str] = mapped_column(Text, default="{}")  # 各分项明细 JSON
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto|manual


# --------------------------------------------------------- 13 agent_runs
class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_task", "task_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(32), default="")
    user_id: Mapped[str] = mapped_column(String(32), default="")
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # recommend|solution|judge|mastery|roadmap
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    model: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|degraded|failed
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(120), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    output: Mapped[str] = mapped_column(Text, default="{}")


# --------------------------------------------------- 14 background_tasks
class BackgroundTask(Base, TimestampMixin):
    __tablename__ = "background_tasks"
    __table_args__ = (Index("ix_bg_status", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(32), default="agent_pipeline")
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|running|done|failed|cancelled|interrupted
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str] = mapped_column(String(64), default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ----------------------------------------------------- 15 daily_checkins
class DailyCheckin(Base, TimestampMixin):
    """每日打卡记录（批次 C / F14）。

    日期用"用户本地时区的日期"而非 UTC 日期 —— 否则东八区用户在
    早上 8 点前打卡会被算到前一天，连续天数会莫名中断。
    checkin_date 存 YYYY-MM-DD 字符串，与 User.timezone 配合使用。
    """

    __tablename__ = "daily_checkins"
    __table_args__ = (
        UniqueConstraint("user_id", "checkin_date", name="uq_checkin_user_date"),
        Index("ix_checkin_user_date", "user_id", "checkin_date"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    checkin_date: Mapped[str] = mapped_column(String(10), nullable=False)  # 用户本地日期
    ac_count: Mapped[int] = mapped_column(Integer, default=0)  # 当日通过题数
    submit_count: Mapped[int] = mapped_column(Integer, default=0)  # 当日提交数
    goal_met: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否达成每日目标
    note: Mapped[str] = mapped_column(String(200), default="")


# ---------------------------------------------------- 16 notifications
class Notification(Base, TimestampMixin):
    """站内通知（批次 C / F16）：复习提醒、目标达成、系统公告。"""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_read", "user_id", "read_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24), default="review")  # review|goal|system
    title: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # 跳转目标（前端路由，如 /problems/xxx）
    link: Mapped[str] = mapped_column(String(200), default="")
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
