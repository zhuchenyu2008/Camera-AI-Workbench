"""独立模型价格资源、汇率缓存和用量费用快照。"""

import json
import time

from alembic import op
import sqlalchemy as sa
from sqlalchemy import BigInteger, Column, Integer, JSON, String

from app.repositories.schema_v4 import exchange_rates, provider_pricing

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    provider_pricing.create(bind)
    exchange_rates.create(bind)

    with op.batch_alter_table("ai_usage") as batch:
        batch.add_column(Column("pricing_revision", Integer))
        batch.add_column(Column("price_snapshot_json", JSON))
        batch.add_column(Column("exchange_rate_snapshot_json", JSON))
        batch.add_column(Column("estimated_cost", String))
        batch.add_column(Column("estimated_cost_cny", String))
        batch.add_column(Column("cost_status", String, nullable=False, server_default="unpriced"))
        batch.add_column(Column("cost_reason", String))
        batch.create_check_constraint(
            "v4_ai_usage_price_snapshot_json",
            "price_snapshot_json IS NULL OR json_valid(price_snapshot_json)",
        )
        batch.create_check_constraint(
            "v4_ai_usage_exchange_rate_snapshot_json",
            "exchange_rate_snapshot_json IS NULL OR json_valid(exchange_rate_snapshot_json)",
        )
        batch.create_check_constraint(
            "v4_ai_usage_cost_status",
            "cost_status IN ('unpriced', 'unconverted', 'converted')",
        )

    # 为旧 Provider 初始化“未配置价格”的资源
    now = int(time.time() * 1000)
    providers = bind.execute(sa.text("SELECT id FROM providers")).fetchall()
    for (pid,) in providers:
        bind.execute(
            sa.text(
                "INSERT INTO provider_pricing (id, provider_id, revision, created_at_ms, updated_at_ms) "
                "VALUES (:id, :provider_id, 1, :now, :now)"
            ),
            {"id": f"prc_{pid}", "provider_id": pid, "now": now},
        )

    # 清理当前设置 JSON 中通知和预算的占位键，保留历史审计内容
    settings_rows = bind.execute(sa.text("SELECT id, config_json FROM settings")).fetchall()
    for sid, cfg_raw in settings_rows:
        cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else dict(cfg_raw)
        cfg.pop("notification_enabled", None)
        cfg.pop("budget_limit", None)
        bind.execute(
            sa.text("UPDATE settings SET config_json = :cfg WHERE id = :id"),
            {"cfg": json.dumps(cfg), "id": sid},
        )


def downgrade():
    bind = op.get_bind()
    with op.batch_alter_table("ai_usage") as batch:
        batch.drop_constraint("v4_ai_usage_cost_status", type_="check")
        batch.drop_constraint("v4_ai_usage_exchange_rate_snapshot_json", type_="check")
        batch.drop_constraint("v4_ai_usage_price_snapshot_json", type_="check")
        batch.drop_column("cost_reason")
        batch.drop_column("cost_status")
        batch.drop_column("estimated_cost_cny")
        batch.drop_column("estimated_cost")
        batch.drop_column("exchange_rate_snapshot_json")
        batch.drop_column("price_snapshot_json")
        batch.drop_column("pricing_revision")

    exchange_rates.drop(bind)
    provider_pricing.drop(bind)
