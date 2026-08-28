from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.db import DiagnosisRun, IncidentEvent, get_session
from app.domain import DiagnosisStatus, IncidentStatus
from app.schemas import (
    DiagnosisCreate,
    DiagnosisDetail,
    DiagnosisRunRead,
    EvidenceRead,
    HypothesisRead,
    IncidentListResponse,
    IncidentRead,
)
from app.security import AuthenticatedUser, operator_required, viewer_required
from app.services.incident_repository import (
    diagnosis_to_dict,
    incident_repository,
    incident_to_dict,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])
diagnoses_router = APIRouter(prefix="/diagnoses", tags=["diagnoses"])


@router.get("", response_model=IncidentListResponse)
async def list_incidents(
    incident_status: IncidentStatus | None = Query(default=None, alias="status"),
    service_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> IncidentListResponse:
    rows, total = await incident_repository.list_incidents(
        session,
        status=incident_status,
        service_name=service_name,
        limit=limit,
        offset=offset,
    )
    return IncidentListResponse(
        items=[IncidentRead.model_validate(incident_to_dict(item)) for item in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{incident_id}", response_model=IncidentRead)
async def get_incident(
    incident_id: str,
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> IncidentRead:
    incident = await incident_repository.get_incident(session, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return cast(IncidentRead, IncidentRead.model_validate(incident_to_dict(incident)))


@router.post(
    "/{incident_id}/diagnoses",
    response_model=DiagnosisRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_diagnosis(
    incident_id: str,
    payload: DiagnosisCreate,
    current_user: AuthenticatedUser = Depends(operator_required),
    session: AsyncSession = Depends(get_session),
) -> DiagnosisRunRead:
    incident = await incident_repository.get_incident(session, incident_id, with_details=False)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    idempotency_key = payload.idempotency_key or f"manual:{uuid.uuid4()}"
    run, _created = await incident_repository.queue_diagnosis(
        session,
        incident,
        trigger="manual",
        requested_by=current_user.username,
        idempotency_key=idempotency_key,
    )
    await session.commit()
    return cast(DiagnosisRunRead, DiagnosisRunRead.model_validate(diagnosis_to_dict(run)))


@diagnoses_router.get("/{run_id}", response_model=DiagnosisDetail)
async def get_diagnosis(
    run_id: str,
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> DiagnosisDetail:
    run = await session.scalar(
        select(DiagnosisRun)
        .where(DiagnosisRun.id == run_id)
        .options(
            selectinload(DiagnosisRun.evidence),
            selectinload(DiagnosisRun.hypotheses),
            selectinload(DiagnosisRun.tool_calls),
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="diagnosis run not found")
    base = diagnosis_to_dict(run)
    base["evidence"] = [
        EvidenceRead(
            id=item.id,
            source=item.source,
            kind=item.kind,
            status=item.status,
            summary=item.summary,
            query=item.query,
            data=item.data,
            observed_at=item.observed_at,
        )
        for item in run.evidence
    ]
    base["hypotheses"] = [
        HypothesisRead(
            id=item.id,
            rank=item.rank,
            category=item.category,
            title=item.title,
            confidence=item.confidence,
            verdict=item.verdict,
            supporting_evidence_ids=item.supporting_evidence_ids,
            contradicting_evidence_ids=item.contradicting_evidence_ids,
        )
        for item in sorted(run.hypotheses, key=lambda value: value.rank)
    ]
    base["tool_calls"] = [
        {
            "id": item.id,
            "tool_name": item.tool_name,
            "source": item.source,
            "risk_level": item.risk_level.value,
            "status": item.status,
            "input": item.input,
            "output": item.output,
            "error": item.error,
            "duration_ms": item.duration_ms,
        }
        for item in run.tool_calls
    ]
    return cast(DiagnosisDetail, DiagnosisDetail.model_validate(base))


@diagnoses_router.get("/{run_id}/events")
async def diagnosis_events(
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _current_user: AuthenticatedUser = Depends(viewer_required),
) -> EventSourceResponse:
    try:
        cursor = int(last_event_id or 0)
    except ValueError:
        cursor = 0

    async def generate() -> AsyncIterator[dict[str, Any]]:
        nonlocal cursor
        while True:
            async for session in get_session():
                run = await session.get(DiagnosisRun, run_id)
                if run is None:
                    yield {"event": "error", "data": json.dumps({"detail": "run not found"})}
                    return
                query = (
                    select(IncidentEvent)
                    .where(IncidentEvent.incident_id == run.incident_id, IncidentEvent.id > cursor)
                    .order_by(IncidentEvent.id)
                )
                events = list((await session.scalars(query)).all())
                for event in events:
                    cursor = event.id
                    yield {
                        "id": str(event.id),
                        "event": event.event_type,
                        "data": json.dumps(
                            {
                                "id": event.id,
                                "message": event.message,
                                "payload": event.payload,
                                "created_at": event.created_at.isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                    }
                terminal = run.status in {
                    DiagnosisStatus.DIAGNOSED,
                    DiagnosisStatus.INCONCLUSIVE,
                    DiagnosisStatus.FAILED,
                }
            if terminal and not events:
                yield {"event": "complete", "data": json.dumps({"status": run.status.value})}
                return
            await asyncio.sleep(1)

    return EventSourceResponse(generate())
