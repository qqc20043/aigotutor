"""学习计划（对标 LeetCode Study Plan）：study_plans + study_plan_items。

零报名制：计划只是"题目清单 + 展示元数据"，用户进度由提交记录派生
（某题有 AC 即完成），不建 enroll 表——与 XP/成就"从事实表派生"的
原则一致，无用户状态迁移问题。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a4c1f8e2b9"
down_revision: str | None = "c3f8a2d94e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "study_plans",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("subtitle", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="精选"),
        sa.Column("theme", sa.String(length=16), nullable=False, server_default="green"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_study_plans_category", "study_plans", ["category", "sort_order"], unique=False
    )
    op.create_table(
        "study_plan_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("problem_id", sa.String(length=32), nullable=False),
        sa.Column("order_no", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["plan_id"], ["study_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_study_plan_items_plan", "study_plan_items", ["plan_id", "order_no"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_study_plan_items_plan", table_name="study_plan_items")
    op.drop_table("study_plan_items")
    op.drop_index("ix_study_plans_category", table_name="study_plans")
    op.drop_table("study_plans")
