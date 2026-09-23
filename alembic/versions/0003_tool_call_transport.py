"""persist the transport used by each observability tool call

Revision ID: 0003
Revises: 0002
"""

from alembic import op
from sqlalchemy import Column, String, inspect


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有工具调用记录增加传输通道字段，旧记录默认为 local。"""
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("tool_call_records")}
    if "transport" not in columns:
        op.add_column(
            "tool_call_records",
            Column("transport", String(length=20), nullable=False, server_default="local"),
        )


def downgrade() -> None:
    """只回滚本次新增的审计字段，不删除工具调用记录。"""
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("tool_call_records")}
    if "transport" in columns:
        op.drop_column("tool_call_records", "transport")
