"""完整初始结构，包括短期帧索引与长期覆盖、证据血缘。"""

from alembic import op

from app.repositories.schema_v1 import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
