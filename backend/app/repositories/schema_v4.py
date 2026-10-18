"""0004 的冻结结构，补全模型独立价格、汇率缓存和用量费用快照。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
)

from app.repositories import schema_v3

metadata = MetaData()
for original in schema_v3.metadata.tables.values():
    globals()[original.name] = original.to_metadata(metadata)

provider_pricing = Table(
    "provider_pricing",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider_id", String, ForeignKey("providers.id"), nullable=False, unique=True, index=True),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("base_url", String),
    Column("model", String),
    Column("currency", String),
    Column("input_price", String),
    Column("output_price", String),
    Column("cached_input_price", String),
    Column("source_note", String),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    CheckConstraint("currency IS NULL OR currency IN ('CNY', 'USD')", name="v4_pricing_currency"),
    CheckConstraint("revision > 0", name="v4_pricing_revision_positive"),
)

exchange_rates = Table(
    "exchange_rates",
    metadata,
    Column("id", String, primary_key=True),
    Column("from_currency", String, nullable=False, server_default="'USD'"),
    Column("to_currency", String, nullable=False, server_default="'CNY'"),
    Column("rate", String, nullable=False),
    Column("rate_date", String, nullable=False),
    Column("source", String, nullable=False),
    Column("fetched_at_ms", BigInteger, nullable=False),
    Column("raw_data_json", JSON),
    CheckConstraint("raw_data_json IS NULL OR json_valid(raw_data_json)", name="v4_exchange_rates_raw_json"),
)

usage = metadata.tables["ai_usage"]
usage.append_column(Column("pricing_revision", Integer))
usage.append_column(Column("price_snapshot_json", JSON))
usage.append_column(Column("exchange_rate_snapshot_json", JSON))
usage.append_column(Column("estimated_cost", String))
usage.append_column(Column("estimated_cost_cny", String))
usage.append_column(Column("cost_status", String, nullable=False, server_default="'unpriced'"))
usage.append_column(Column("cost_reason", String))

usage.append_constraint(
    CheckConstraint(
        "price_snapshot_json IS NULL OR json_valid(price_snapshot_json)",
        name="v4_ai_usage_price_snapshot_json",
    )
)
usage.append_constraint(
    CheckConstraint(
        "exchange_rate_snapshot_json IS NULL OR json_valid(exchange_rate_snapshot_json)",
        name="v4_ai_usage_exchange_rate_snapshot_json",
    )
)
usage.append_constraint(
    CheckConstraint(
        "cost_status IN ('unpriced', 'unconverted', 'converted')",
        name="v4_ai_usage_cost_status",
    )
)
