"""模拟面试：chat_sessions 增列（kind/time_limit_minutes/meta）+ interview_reports 表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c31f8a9d02"
down_revision: str | None = "c8f1a37d02b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="tutor")
        )
        batch_op.add_column(
            sa.Column("time_limit_minutes", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("meta", sa.Text(), nullable=False, server_default="{}"))

    op.create_table(
        "interview_reports",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("problem_id", sa.String(length=32), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("scores", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("interview_reports", schema=None) as batch_op:
        batch_op.create_index("ix_interview_reports_user", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("interview_reports")
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.drop_column("meta")
        batch_op.drop_column("time_limit_minutes")
        batch_op.drop_column("kind")
