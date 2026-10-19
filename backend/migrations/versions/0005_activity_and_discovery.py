"""以活动为核心，增加可修订的自动档案。"""

from alembic import op
from sqlalchemy import BigInteger, Boolean, Column, String

from app.repositories.schema_v5 import (
    discovery_candidates,
    entity_changes,
    entity_occurrences,
    entity_separations,
)

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("entities") as batch:
        batch.add_column(Column("origin", String, nullable=False, server_default="manual"))
        batch.add_column(Column("reviewed", Boolean, nullable=False, server_default="1"))
        batch.add_column(Column("merged_into_id", String))
        batch.add_column(Column("last_seen_at_ms", BigInteger))
    with op.batch_alter_table("reference_assets") as batch:
        batch.add_column(Column("source_candidate_key", String))
    for table in (entity_occurrences, entity_changes, entity_separations, discovery_candidates):
        table.create(op.get_bind())


def downgrade():
    for table in (discovery_candidates, entity_separations, entity_changes, entity_occurrences):
        table.drop(op.get_bind())
    with op.batch_alter_table("reference_assets") as batch:
        batch.drop_column("source_candidate_key")
    with op.batch_alter_table("entities") as batch:
        for name in ("last_seen_at_ms", "merged_into_id", "reviewed", "origin"):
            batch.drop_column(name)
