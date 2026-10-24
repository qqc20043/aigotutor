"""leech 搁置语义（review_queue.suspended）+ 自定义练习配方（practice_sets）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a9c31e5b77"
down_revision: str | None = "e7c31f8a9d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("review_queue", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("suspended", sa.Boolean(), nullable=False, server_default="0")
        )

    op.create_table(
        "practice_sets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("recipe", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("practice_sets", schema=None) as batch_op:
        batch_op.create_index("ix_practice_sets_user", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("practice_sets")
    with op.batch_alter_table("review_queue", schema=None) as batch_op:
        batch_op.drop_column("suspended")
