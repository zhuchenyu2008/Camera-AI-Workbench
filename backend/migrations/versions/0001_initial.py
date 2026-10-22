"""Initial SQLite/PostgreSQL schema.

The first migration deliberately delegates DDL generation to the declarative
metadata so the initial install stays exactly aligned with the v1.0 model set.
Future revisions must use explicit Alembic operations and remain immutable.
"""
from __future__ import annotations

from alembic import op

from app.db import Base
from app import models  # noqa: F401 -- register every v1 table

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Schema rollback is intentionally forward-only: dropping application tables
    # would destroy the user's camera history. Restore a pre-migration backup
    # after stopping all services if a rollback is required.
    raise RuntimeError("0001_initial is forward-only; refusing to drop user data")
