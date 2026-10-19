"""0006：人物参考档案模式和分钟决策审计。"""

from sqlalchemy import JSON, Column

from app.repositories import schema_v5

metadata = schema_v5.metadata
for name, table in metadata.tables.items():
    globals()[name] = table

# Tool calls are optional and read-only, but their full audit trail belongs to
# the attempt that made them so it remains available after the minute result is
# projected.
metadata.tables["job_attempts"].append_column(Column("tool_audit_json", JSON))
