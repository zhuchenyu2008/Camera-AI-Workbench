"""Add queue lease fencing and deadline columns to existing databases.

The first revision intentionally uses ``Base.metadata.create_all`` so a fresh
installation includes the complete v1 schema.  Older installations created
before lease fencing was added may already be stamped at 0001, therefore this
revision is defensive and only adds columns that are not present.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_job_lease_fencing"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if "ai_job" not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind, "ai_job")
    missing = []
    if "lease_token" not in columns:
        missing.append(sa.Column("lease_token", sa.String(length=256), nullable=True))
    if "deadline_at_ms" not in columns:
        missing.append(sa.Column("deadline_at_ms", sa.BigInteger(), nullable=True))
    if not missing:
        return
    with op.batch_alter_table("ai_job", recreate="auto") as batch:
        for column in missing:
            batch.add_column(column)


def downgrade() -> None:
    # Leaving nullable metadata columns is safe for older application builds and
    # avoids dropping in-flight lease/deadline information during a rollback.
    return None
