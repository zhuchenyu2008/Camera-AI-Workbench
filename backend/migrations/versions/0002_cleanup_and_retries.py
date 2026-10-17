"""物理清理进度与重试计数独立于请求用量。"""

from alembic import op
from sqlalchemy import BigInteger, Column, Integer

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("frames", Column("file_deleted_at_ms", BigInteger))
    op.add_column("jobs", Column("failure_count", Integer, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("jobs", "failure_count")
    op.drop_column("frames", "file_deleted_at_ms")
