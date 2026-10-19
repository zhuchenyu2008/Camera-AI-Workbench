"""当前结构。每次迁移引用各自冻结的结构。"""

from app.repositories import schema_v5

metadata = schema_v5.metadata
for name, table in metadata.tables.items():
    globals()[name] = table
