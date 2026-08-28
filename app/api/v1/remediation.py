from __future__ import annotations

from typing import cast

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.db import AuditEvent, Incident, RemediationProposal, get_session, utcnow
from app.domain import (
    IncidentStatus,
    ProposalStatus,
    RiskLevel,
    Role,
    ensure_incident_transition,
)
from app.schemas import RemediationDecision, RemediationProposalRead
from app.security import AuthenticatedUser, operator_required
from app.services.remediation_service import remediation_service

router = APIRouter(prefix="/remediation-proposals", tags=["remediation"])


def _read(proposal: RemediationProposal) -> RemediationProposalRead:
    return RemediationProposalRead(
        id=proposal.id,
        incident_id=proposal.incident_id,
        action_id=proposal.action_id,
        parameters=proposal.parameters,
        reason=proposal.reason,
        risk_level=proposal.risk_level,
        status=proposal.status,
        proposed_by=proposal.proposed_by,
        decided_by=proposal.decided_by,
        result=proposal.result,
        created_at=proposal.created_at,
    )


async def _get_pending(session: AsyncSession, proposal_id: str) -> RemediationProposal:
    proposal = await session.get(RemediationProposal, proposal_id, with_for_update=True)
    if proposal is None:
        raise HTTPException(status_code=404, detail="remediation proposal not found")
    if proposal.status != ProposalStatus.PENDING:
        raise HTTPException(status_code=409, detail="proposal has already been decided")
    return cast(RemediationProposal, proposal)


@router.post("/{proposal_id}/approve", response_model=RemediationProposalRead)
async def approve(
    proposal_id: str,
    decision: RemediationDecision,
    current_user: AuthenticatedUser = Depends(operator_required),
    session: AsyncSession = Depends(get_session),
) -> RemediationProposalRead:
    proposal = await _get_pending(session, proposal_id)
    if not config.lab_mode or not config.allow_mutations:
        raise HTTPException(status_code=403, detail="mutations are disabled outside the lab")
    if proposal.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH} and current_user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="admin approval is required for this risk level")
    proposal.status = ProposalStatus.APPROVED
    proposal.decided_by = current_user.username
    proposal.decided_at = utcnow()
    proposal.result = {"decision_reason": decision.reason, "execution": "queued"}
    session.add(
        AuditEvent(
            actor=current_user.username,
            action="remediation.approve",
            resource_type="remediation_proposal",
            resource_id=proposal.id,
            outcome="approved",
            detail={"reason": decision.reason, "action_id": proposal.action_id},
        )
    )
    await session.commit()
    try:
        executed = await remediation_service.execute(proposal.id, current_user.username)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (LookupError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _read(executed)


@router.post("/{proposal_id}/reject", response_model=RemediationProposalRead)
async def reject(
    proposal_id: str,
    decision: RemediationDecision,
    current_user: AuthenticatedUser = Depends(operator_required),
    session: AsyncSession = Depends(get_session),
) -> RemediationProposalRead:
    proposal = await _get_pending(session, proposal_id)
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by = current_user.username
    proposal.decided_at = utcnow()
    proposal.result = {"decision_reason": decision.reason}
    incident = await session.get(Incident, proposal.incident_id, with_for_update=True)
    if incident is not None and incident.status == IncidentStatus.WAITING_APPROVAL:
        ensure_incident_transition(incident.status, IncidentStatus.DIAGNOSED)
        incident.status = IncidentStatus.DIAGNOSED
    session.add(
        AuditEvent(
            actor=current_user.username,
            action="remediation.reject",
            resource_type="remediation_proposal",
            resource_id=proposal.id,
            outcome="rejected",
            detail={"reason": decision.reason},
        )
    )
    await session.commit()
    await session.refresh(proposal)
    return _read(proposal)
