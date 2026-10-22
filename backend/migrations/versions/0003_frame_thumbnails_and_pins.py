"""Add frame thumbnail/signal metadata and bounded task pinning."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_frame_thumbnails_and_pins"
down_revision = "0002_job_lease_fencing"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    if "frame" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("frame")}
    missing = [
        sa.Column("thumbnail_relative_path", sa.String(length=1024), nullable=True),
        sa.Column("local_signal_json", sa.JSON(), nullable=True),
        sa.Column("pin_until_ms", sa.BigInteger(), nullable=True),
    ]
    with op.batch_alter_table("frame", recreate="auto") as batch:
        for column in missing:
            if column.name not in columns:
                batch.add_column(column)

def downgrade() -> None:
    return None
