"""0003 的冻结结构，补全设计中的 JSON、状态及延迟版本外键约束。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
)

from app.repositories import schema_v1

metadata = MetaData()
for original in schema_v1.metadata.sorted_tables:
    globals()[original.name] = original.to_metadata(metadata)

metadata.tables["frames"].append_column(Column("file_deleted_at_ms", BigInteger))
metadata.tables["jobs"].append_column(Column("failure_count", Integer, nullable=False, server_default="0"))
for table in metadata.tables.values():
    for column in table.columns:
        if isinstance(column.type, JSON):
            expression = (
                f"json_valid({column.name})"
                if not column.nullable
                else f"{column.name} IS NULL OR json_valid({column.name})"
            )
            table.append_constraint(CheckConstraint(expression, name=f"v3_{table.name}_{column.name}_json"))
        elif isinstance(column.type, Boolean):
            table.append_constraint(
                CheckConstraint(f"{column.name} IN (0,1)", name=f"v3_{table.name}_{column.name}_bool")
            )

minutes = metadata.tables["minute_records"]
minutes.c.current_revision.nullable = True
minutes.c.current_revision.server_default = None
minutes.append_constraint(CheckConstraint("start_at_ms % 60000 = 0", name="v3_minute_aligned"))
minutes.append_constraint(
    CheckConstraint("current_revision IS NULL OR current_revision > 0", name="v3_current_positive")
)
minutes.append_constraint(
    CheckConstraint(
        "analysis_state IN ('pending','running','complete','partial','failed','expired')",
        name="v3_minute_state",
    )
)
minutes.append_constraint(
    ForeignKeyConstraint(
        ["id", "current_revision"],
        ["minute_revisions.minute_id", "minute_revisions.revision"],
        name="v3_minute_current",
        deferrable=True,
        initially="DEFERRED",
    )
)
revisions = metadata.tables["minute_revisions"]
revisions.append_constraint(CheckConstraint("revision > 0", name="v3_minute_revision_positive"))
revisions.append_constraint(
    CheckConstraint("source_kind IN ('ai','manual','reconcile')", name="v3_minute_source_kind")
)
metadata.tables["evidence_refs"].append_constraint(
    CheckConstraint("state IN ('available','expired','evicted','missing')", name="v3_evidence_state")
)
for name in ["planned_bitmap", "captured_bitmap", "evicted_bitmap", "missing_bitmap"]:
    metadata.tables["minute_capture"].append_constraint(
        CheckConstraint(f"{name} BETWEEN 0 AND 1152921504606846975", name=f"v3_{name}_60bits")
    )
