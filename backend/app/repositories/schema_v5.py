"""0005：自动档案、出现记录和人工关联修订。"""

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, ForeignKey, MetaData, String, Table

from app.repositories import schema_v4

metadata = MetaData()
for original in schema_v4.metadata.tables.values():
    globals()[original.name] = original.to_metadata(metadata)

entities = metadata.tables["entities"]
entities.append_column(Column("origin", String, nullable=False, server_default="manual"))
entities.append_column(Column("reviewed", Boolean, nullable=False, server_default="1"))
entities.append_column(Column("merged_into_id", String))
entities.append_column(Column("last_seen_at_ms", BigInteger))
metadata.tables["reference_assets"].append_column(Column("source_candidate_key", String))

entity_occurrences = Table(
    "entity_occurrences",
    metadata,
    Column("id", String, primary_key=True),
    Column("entity_id", String, ForeignKey("entities.id"), nullable=False, index=True),
    Column("source_key", String, nullable=False, unique=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False),
    Column("captured_at_ms", BigInteger, nullable=False, index=True),
    Column("evidence_id", String, nullable=False),
    Column("crop_json", JSON, nullable=False),
    Column("links_json", JSON, nullable=False),
    Column("manual", Boolean, nullable=False, server_default="0"),
    Column("created_at_ms", BigInteger, nullable=False),
    CheckConstraint("json_valid(crop_json) AND json_valid(links_json)", name="v5_occurrence_json"),
)
entity_changes = Table(
    "entity_changes",
    metadata,
    Column("id", String, primary_key=True),
    Column("entity_id", String, ForeignKey("entities.id"), nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("details_json", JSON, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
)
entity_separations = Table(
    "entity_separations",
    metadata,
    Column("left_id", String, ForeignKey("entities.id"), primary_key=True),
    Column("right_id", String, ForeignKey("entities.id"), primary_key=True),
)
discovery_candidates = Table(
    "discovery_candidates",
    metadata,
    Column("id", String, primary_key=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False),
    Column("captured_at_ms", BigInteger, nullable=False),
    Column("expires_at_ms", BigInteger, nullable=False),
    Column("candidate_json", JSON, nullable=False),
    Column("prepared_json", JSON),
    Column("links_json", JSON, nullable=False),
    Column("state", String, nullable=False, server_default="pending"),
    Column("entity_id", String),
    Column("created_at_ms", BigInteger, nullable=False),
)
