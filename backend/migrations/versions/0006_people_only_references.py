"""清理物品档案，统一进入人物参考模式。

历史活动文字和人物关系保留；物品实体、参考资产、候选和 JSON 关联不可逆删除。
"""

import json

import sqlalchemy as sa
from alembic import op

from app.security.secrets import fingerprint

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _scrub(value):
    """Remove legacy object associations without changing unrelated JSON."""
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "object_ids":
            result[key] = []
        elif key == "object_candidate_keys":
            result[key] = []
        elif key == "manual_object_assignments":
            result[key] = {}
        else:
            result[key] = _scrub(item)
    return result


def _rewrite_json(bind, table, column, transform):
    rows = bind.execute(sa.text(f"SELECT rowid, {column} FROM {table}")).fetchall()
    for rowid, raw in rows:
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            value = transform(value)
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :value WHERE rowid = :rowid"),
                {"value": encoded, "rowid": rowid},
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue


def upgrade():
    bind = op.get_bind()

    # The column is intentionally added here instead of relying on metadata
    # creation so upgrades of an existing installation retain attempt history.
    columns = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(job_attempts)"))}
    if "tool_audit_json" not in columns:
        with op.batch_alter_table("job_attempts") as batch:
            batch.add_column(sa.Column("tool_audit_json", sa.JSON))

    old_reference_sets = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT id FROM reference_sets WHERE EXISTS "
                "(SELECT 1 FROM json_each(members_json) WHERE json_extract(value, '$.kind') = 'object')"
            )
        )
    }
    if old_reference_sets:
        ref_marks = ",".join(f":ref{i}" for i in range(len(old_reference_sets)))
        ref_params = {f"ref{i}": value for i, value in enumerate(old_reference_sets)}
        bind.execute(
            sa.text(
                f"UPDATE jobs SET state = 'cancelled', phase = 'cancelled', finished_at_ms = strftime('%s','now') * 1000, error_json = :error "
                f"WHERE state IN ('queued','running') AND json_extract(payload_json, '$.reference_set_id') IN ({ref_marks})"
            ),
            {
                **ref_params,
                "error": json.dumps(
                    {"code": "REFERENCE_SET_CHANGED", "message": "物品参考档案已移除"},
                    ensure_ascii=False,
                ),
            },
        )
        bind.execute(
            sa.text(
                f"UPDATE job_attempts SET state = 'failed', error_code = 'REFERENCE_SET_CHANGED', "
                f"finished_at_ms = strftime('%s','now') * 1000 WHERE job_id IN "
                f"(SELECT id FROM jobs WHERE json_extract(payload_json, '$.reference_set_id') IN ({ref_marks}))"
            ),
            ref_params,
        )

    object_ids = [
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM entities WHERE kind = 'object'"))
    ]
    if object_ids:
        marks = ",".join(f":id{i}" for i in range(len(object_ids)))
        params = {f"id{i}": value for i, value in enumerate(object_ids)}

        bind.execute(
            sa.text(
                f"DELETE FROM reference_asset_versions WHERE asset_id IN "
                f"(SELECT id FROM reference_assets WHERE entity_id IN ({marks}))"
            ),
            params,
        )
        bind.execute(
            sa.text(f"DELETE FROM reference_assets WHERE entity_id IN ({marks})"), params
        )
        bind.execute(
            sa.text(f"DELETE FROM entity_occurrences WHERE entity_id IN ({marks})"), params
        )
        bind.execute(
            sa.text(
                "DELETE FROM discovery_candidates WHERE json_extract(candidate_json, '$.kind') = 'object' "
                "OR entity_id IN (" + marks + ")"
            ),
            params,
        )
        bind.execute(
            sa.text(
                f"DELETE FROM entity_separations WHERE left_id IN ({marks}) OR right_id IN ({marks})"
            ),
            params,
        )
        bind.execute(sa.text(f"DELETE FROM entity_changes WHERE entity_id IN ({marks})"), params)
        bind.execute(sa.text(f"DELETE FROM entities WHERE id IN ({marks})"), params)

    # JSON associations have no foreign key. Clean the historical result
    # fields even when the old entity rows were already manually removed.
    for table, column in (
        ("minute_revisions", "result_json"),
        ("event_revisions", "summary_json"),
        ("manual_corrections", "correction_json"),
        ("activity_spans", "details_json"),
    ):
        _rewrite_json(bind, table, column, _scrub)

    # Jobs can contain ordinary payload data, so scrub only payloads that
    # reference an old set and leave unrelated job JSON unchanged.
    _rewrite_json(
        bind,
        "jobs",
        "payload_json",
        lambda value: _scrub(value)
        if isinstance(value, dict) and value.get("reference_set_id") in old_reference_sets
        else value,
    )
    _rewrite_json(
        bind,
        "jobs",
        "result_json",
        lambda value: _scrub(value)
        if isinstance(value, dict) and value.get("reference_set_id") in old_reference_sets
        else value,
    )

    def people_only(value):
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict) and item.get("kind") == "person"]

    for rowid, raw in bind.execute(sa.text("SELECT rowid, members_json FROM reference_sets")):
        try:
            members = people_only(json.loads(raw) if isinstance(raw, str) else raw)
            encoded = json.dumps(members, ensure_ascii=False, separators=(",", ":"))
            ref_fingerprint = fingerprint(members)
            if bind.execute(
                sa.text("SELECT 1 FROM reference_sets WHERE fingerprint = :fingerprint AND rowid <> :rowid"),
                {"fingerprint": ref_fingerprint, "rowid": rowid},
            ).first():
                ref_fingerprint = f"{ref_fingerprint}:{rowid}"
            bind.execute(
                sa.text(
                    "UPDATE reference_sets SET members_json = :members, fingerprint = :fingerprint "
                    "WHERE rowid = :rowid"
                ),
                {"members": encoded, "fingerprint": ref_fingerprint, "rowid": rowid},
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue


def downgrade():
    raise RuntimeError("0006 删除的物品档案无法从备份以外恢复")
