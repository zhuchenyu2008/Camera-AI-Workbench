"""完整 JSON / 状态约束和分钟当前版本的延迟外键。"""

from alembic import op
from sqlalchemy import CheckConstraint

from app.repositories.schema_v3 import metadata

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE minute_records SET current_revision = NULL WHERE current_revision = 0")
    op.execute("UPDATE minute_revisions SET source_kind = 'reconcile' WHERE source_kind = 'rules'")
    for table in metadata.tables.values():
        checks = [
            c
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name and c.name.startswith("v3_")
        ]
        if not checks and table.name != "minute_records":
            continue
        # Alembic 批处理不会自动保存无名称 CHECK，显式保留最初的约束。
        original_checks = tuple(
            CheckConstraint(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint) and not c.name
        )
        with op.batch_alter_table(table.name, recreate="always", table_args=original_checks) as batch:
            if table.name == "minute_records":
                batch.alter_column("current_revision", nullable=True, server_default=None)
                batch.create_foreign_key(
                    "v3_minute_current",
                    "minute_revisions",
                    ["id", "current_revision"],
                    ["minute_id", "revision"],
                    deferrable=True,
                    initially="DEFERRED",
                )
            for constraint in checks:
                batch.create_check_constraint(constraint.name, str(constraint.sqltext))


def downgrade():
    for table in reversed(list(metadata.tables.values())):
        checks = [
            c
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name and c.name.startswith("v3_")
        ]
        if not checks and table.name != "minute_records":
            continue
        original_checks = tuple(
            CheckConstraint(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint) and not c.name
        )
        with op.batch_alter_table(table.name, recreate="always", table_args=original_checks) as batch:
            for constraint in checks:
                batch.drop_constraint(constraint.name, type_="check")
            if table.name == "minute_records":
                batch.drop_constraint("v3_minute_current", type_="foreignkey")
    op.execute("UPDATE minute_records SET current_revision = 0 WHERE current_revision IS NULL")
    with op.batch_alter_table("minute_records", recreate="always") as batch:
        batch.alter_column("current_revision", nullable=False, server_default="0")
