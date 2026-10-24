"""Add camera configuration versions and explicit audit actors."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_camera_versions_and_audit_actor"
down_revision = "0004_provider_local_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "camera" in tables:
        columns = {column["name"] for column in inspector.get_columns("camera")}
        if "config_version" not in columns:
            with op.batch_alter_table("camera", recreate="auto") as batch:
                batch.add_column(sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"))
    if "audit_log" in tables:
        columns = {column["name"] for column in inspector.get_columns("audit_log")}
        if "actor_id" not in columns:
            with op.batch_alter_table("audit_log", recreate="auto") as batch:
                batch.add_column(sa.Column("actor_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    # Audit actor information and configuration versions are additive safety
    # metadata. Removing them would weaken concurrency and traceability.
    return None
