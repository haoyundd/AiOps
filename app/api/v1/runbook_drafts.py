"""诊断复盘草稿的查询与管理员审核接口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import RunbookDraft, get_session
from app.domain import RunbookDraftStatus
from app.schemas import RunbookDraftDecision, RunbookDraftRead
from app.security import AuthenticatedUser, admin_required, viewer_required
from app.services.incident_repository import incident_repository
from app.services.runbook_service import runbook_service

router = APIRouter(prefix="/runbook-drafts", tags=["runbook-drafts"])


def _to_read(draft: RunbookDraft) -> RunbookDraftRead:
    """把 ORM 草稿转换为稳定的 API 契约。"""
    return cast(
        RunbookDraftRead,
        RunbookDraftRead.model_validate(
            {
                "id": draft.id,
                "incident_id": draft.incident_id,
                "diagnosis_run_id": draft.diagnosis_run_id,
                "title": draft.title,
                "service_name": draft.service_name,
                "tags": draft.tags,
                "content": draft.content,
                "checksum": draft.checksum,
                "status": draft.status,
                "created_by": draft.created_by,
                "reviewed_by": draft.reviewed_by,
                "review_reason": draft.review_reason,
                "created_at": draft.created_at,
                "reviewed_at": draft.reviewed_at,
            }
        ),
    )


@router.get("", response_model=list[RunbookDraftRead])
async def list_runbook_drafts(
    draft_status: RunbookDraftStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> list[RunbookDraftRead]:
    """按审核状态查询诊断复盘草稿。"""
    query = select(RunbookDraft).order_by(RunbookDraft.created_at.desc()).limit(limit)
    if draft_status is not None:
        query = query.where(RunbookDraft.status == draft_status)
    rows = list((await session.scalars(query)).all())
    return [_to_read(row) for row in rows]


@router.post("/{draft_id}/approve")
async def approve_runbook_draft(
    draft_id: str,
    payload: RunbookDraftDecision,
    current_user: AuthenticatedUser = Depends(admin_required),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """批准草稿并通过现有 RunbookService 写入正式知识库。"""
    draft = await session.scalar(
        select(RunbookDraft).where(RunbookDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="runbook draft not found")
    if draft.status != RunbookDraftStatus.PENDING:
        raise HTTPException(status_code=409, detail="runbook draft has already been reviewed")

    document, created = await runbook_service.create(
        title=draft.title,
        service_name=draft.service_name,
        tags=draft.tags,
        content=draft.content,
        created_by=current_user.username,
    )
    draft.status = RunbookDraftStatus.APPROVED
    draft.reviewed_by = current_user.username
    draft.review_reason = payload.reason
    draft.reviewed_at = datetime.now(UTC)
    await incident_repository.append_event(
        session,
        draft.incident_id,
        "runbook_draft_approved",
        "Administrator approved a diagnosis runbook draft",
        payload={"draft_id": draft.id, "runbook_id": document.id, "created": created},
        actor=current_user.username,
    )
    await session.commit()
    return {"draft_id": draft.id, "runbook_id": document.id, "created": created}


@router.post("/{draft_id}/reject")
async def reject_runbook_draft(
    draft_id: str,
    payload: RunbookDraftDecision,
    current_user: AuthenticatedUser = Depends(admin_required),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """驳回草稿并保留审核理由，不向正式知识库写入内容。"""
    draft = await session.scalar(
        select(RunbookDraft).where(RunbookDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="runbook draft not found")
    if draft.status != RunbookDraftStatus.PENDING:
        raise HTTPException(status_code=409, detail="runbook draft has already been reviewed")
    draft.status = RunbookDraftStatus.REJECTED
    draft.reviewed_by = current_user.username
    draft.review_reason = payload.reason
    draft.reviewed_at = datetime.now(UTC)
    await incident_repository.append_event(
        session,
        draft.incident_id,
        "runbook_draft_rejected",
        "Administrator rejected a diagnosis runbook draft",
        payload={"draft_id": draft.id, "reason": payload.reason},
        actor=current_user.username,
    )
    await session.commit()
    return {"draft_id": draft.id, "status": draft.status.value}
