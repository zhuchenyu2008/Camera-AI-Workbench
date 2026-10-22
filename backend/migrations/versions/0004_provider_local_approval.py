"""Persist explicit approval for local/insecure Provider endpoints."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_provider_local_approval"
down_revision = "0003_frame_thumbnails_and_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "ai_provider" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("ai_provider")}
    if "allow_insecure_local" not in columns:
        with op.batch_alter_table("ai_provider", recreate="auto") as batch:
            batch.add_column(sa.Column("allow_insecure_local", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    # Keep the explicit policy bit; dropping it during rollback could silently
    # change access policy or destroy an administrator's security choice.
    return None