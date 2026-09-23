"""bind remediation proposals to the diagnosis run that created them

Revision ID: 0005
Revises: 0004
"""

from alembic import op
from sqlalchemy import Column, ForeignKey, Index, String, inspect


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可为空的诊断运行绑定，不修改历史提案和历史审计数据。"""
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("remediation_proposals")}
    if "diagnosis_run_id" not in columns:
        op.add_column(
            "remediation_proposals",
            Column(
                "diagnosis_run_id",
                String(length=36),
                ForeignKey("diagnosis_runs.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    indexes = {index["name"] for index in inspector.get_indexes("remediation_proposals")}
    if "ix_remediation_proposals_diagnosis_run_id" not in indexes:
        op.create_index(
            "ix_remediation_proposals_diagnosis_run_id",
            "remediation_proposals",
            ["diagnosis_run_id"],
        )


def downgrade() -> None:
    """仅回滚新增索引和列，不删除历史提案主体。"""
    bind = op.get_bind()
    indexes = {index["name"] for index in inspect(bind).get_indexes("remediation_proposals")}
    if "ix_remediation_proposals_diagnosis_run_id" in indexes:
        op.drop_index("ix_remediation_proposals_diagnosis_run_id", table_name="remediation_proposals")
    columns = {column["name"] for column in inspect(bind).get_columns("remediation_proposals")}
    if "diagnosis_run_id" in columns:
        op.drop_column("remediation_proposals", "diagnosis_run_id")
