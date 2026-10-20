"""时间片观察、上级审查、检查点和发布时间线的最小持久化结构。"""

from alembic import op

from app.repositories.schema_v7 import (
    local_observation_runs,
    published_timelines,
    temporal_checkpoints,
    temporal_review_runs,
    temporal_slices,
)

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in (
        temporal_slices,
        local_observation_runs,
        temporal_review_runs,
        temporal_checkpoints,
        published_timelines,
    ):
        table.create(bind)


def downgrade():
    bind = op.get_bind()
    for table in (
        published_timelines,
        temporal_checkpoints,
        temporal_review_runs,
        local_observation_runs,
        temporal_slices,
    ):
        table.drop(bind)
