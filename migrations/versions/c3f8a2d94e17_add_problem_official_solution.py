"""problems.official_solution：官方题解（思路/复杂度/参考代码）。

背景：题解此前只能靠 solution Agent 按提交现编（规则兜底仅 preset 模板）。
经典题应有"经过沙箱验证 AC"的官方题解。详情接口按"AC 过或管理员"门控
下发——题解含完整解法，未做题先看到等于剧透。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f8a2d94e17"
down_revision: str | None = "a3d51c9e7f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("problems", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("official_solution", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("problems", schema=None) as batch_op:
        batch_op.drop_column("official_solution")
