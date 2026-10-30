"""管理操作审计日志：所有管理动作留痕（谁、何时、对什么、做了什么、结果）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d51c9e7f04"
down_revision: str | None = "f0a9c31e5b77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("actor_name", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("admin_audit_logs", schema=None) as batch_op:
        batch_op.create_index("ix_audit_created", ["created_at"])
        batch_op.create_index("ix_audit_actor", ["actor_id", "created_at"])
        batch_op.create_index("ix_audit_action", ["action"])


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
