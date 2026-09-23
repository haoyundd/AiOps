"""persist actual diagnosis runtime metadata and execution statistics

Revision ID: 0004
Revises: 0003
"""

from alembic import op
from sqlalchemy import Column, Integer, String, inspect


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可为空的运行元数据，不修改历史诊断结论。"""
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("diagnosis_runs")}
    if "provider" not in columns:
        op.add_column("diagnosis_runs", Column("provider", String(length=40), nullable=True))
    if "model" not in columns:
        op.add_column("diagnosis_runs", Column("model", String(length=160), nullable=True))
    if "total_steps" not in columns:
        op.add_column("diagnosis_runs", Column("total_steps", Integer(), nullable=True))
    if "total_tool_calls" not in columns:
        op.add_column("diagnosis_runs", Column("total_tool_calls", Integer(), nullable=True))


def downgrade() -> None:
    """只回滚本次新增列，不删除 Incident 或诊断数据。"""
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("diagnosis_runs")}
    for name in ("total_tool_calls", "total_steps", "model", "provider"):
        if name in columns:
            op.drop_column("diagnosis_runs", name)
