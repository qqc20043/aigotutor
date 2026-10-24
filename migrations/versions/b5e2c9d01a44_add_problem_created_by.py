"""problems.created_by：用例写权限按创建者判定。

背景：此前任何登录用户都能增删改任意题目的测试用例（含隐藏期望输出），
等于能伪造判题结果（PoC 已复现）。本迁移给题目加创建者归属；存量行
（种子/导入数据）created_by 为空，仅管理员可改其用例。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e2c9d01a44"
down_revision: str | None = "38cf377b871f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("problems", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("created_by", sa.String(length=32), nullable=False, server_default="")
        )
        batch_op.create_index("ix_problems_created_by", ["created_by"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("problems", schema=None) as batch_op:
        batch_op.drop_index("ix_problems_created_by")
        batch_op.drop_column("created_by")
