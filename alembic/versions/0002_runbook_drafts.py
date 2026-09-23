"""add reviewed runbook drafts generated from diagnoses

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import inspect

from app.db import RunbookDraft


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建知识草稿表；使用 metadata 保持 SQLite 测试和 PostgreSQL 一致。"""
    bind = op.get_bind()
    if not inspect(bind).has_table("runbook_drafts"):
        RunbookDraft.__table__.create(bind=bind)


def downgrade() -> None:
    """回滚时只删除本次新增的草稿表。"""
    op.drop_table("runbook_drafts")
