from sqlalchemy import select

from app.config import config
from app.db import AuditEvent, DiagnosisRun, Incident, RemediationProposal, session_scope
from app.domain import DiagnosisStatus, IncidentStatus, ProposalStatus, RiskLevel
from app.services.incident_repository import incident_repository
from app.services.remediation_service import remediation_service


async def test_allowlisted_lab_recovery_is_verified(monkeypatch):
    monkeypatch.setattr(config, "lab_mode", True)
    monkeypatch.setattr(config, "allow_mutations", True)
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(
            session,
            {
                "status": "firing",
                "fingerprint": "remediation-case",
                "labels": {
                    "alertname": "MerchantFlowHighLatency",
                    "service": "merchantflow",
                    "environment": "test",
                },
            },
        )
        incident.status = IncidentStatus.WAITING_APPROVAL
        run = DiagnosisRun(
            incident_id=incident.id,
            status=DiagnosisStatus.DIAGNOSED,
            idempotency_key="remediation-run-binding",
        )
        session.add(run)
        await session.flush()
        proposal = RemediationProposal(
            incident_id=incident.id,
            diagnosis_run_id=run.id,
            action_id="remove_redis_latency",
            parameters={},
            reason="latency toxic is confirmed",
            risk_level=RiskLevel.LOW,
            status=ProposalStatus.APPROVED,
            idempotency_key="remediation-idempotency",
            decided_by="operator",
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
        incident_id = incident.id

    async def apply_action(_action_id):
        return {"toxic_removed": True}

    async def healthy():
        return {"status": "success", "data": {"state": "UP"}}

    monkeypatch.setattr(remediation_service, "_apply_action", apply_action)
    monkeypatch.setattr(remediation_service, "_verify_health", healthy)
    result = await remediation_service.execute(proposal_id, "operator")
    assert result.status == ProposalStatus.VERIFIED
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.RESOLVED
        proposal = await session.get(RemediationProposal, proposal_id)
        assert proposal is not None
        assert proposal.diagnosis_run_id == run.id
        audit_actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action).where(AuditEvent.resource_id == proposal_id)
                )
            ).all()
        )
        assert audit_actions.count("remediation.execute") == 2


async def test_failed_health_verification_is_persisted_and_audited(monkeypatch):
    monkeypatch.setattr(config, "lab_mode", True)
    monkeypatch.setattr(config, "allow_mutations", True)
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(
            session,
            {
                "status": "firing",
                "fingerprint": "remediation-health-failure",
                "labels": {
                    "alertname": "MerchantFlowHighLatency",
                    "service": "merchantflow",
                    "environment": "test",
                },
            },
        )
        incident.status = IncidentStatus.WAITING_APPROVAL
        proposal = RemediationProposal(
            incident_id=incident.id,
            action_id="remove_redis_latency",
            parameters={},
            reason="verify unhealthy result",
            risk_level=RiskLevel.LOW,
            status=ProposalStatus.APPROVED,
            idempotency_key="remediation-health-failure",
            decided_by="operator",
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
        incident_id = incident.id

    async def apply_action(_action_id):
        return {"toxic_removed": True}

    async def unhealthy():
        return {"status": "error", "data": {"state": "DOWN"}}

    monkeypatch.setattr(remediation_service, "_apply_action", apply_action)
    monkeypatch.setattr(remediation_service, "_verify_health", unhealthy)
    result = await remediation_service.execute(proposal_id, "operator")
    assert result.status == ProposalStatus.FAILED
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.FAILED
        audit = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.resource_id == proposal_id)
                )
            ).all()
        )
        assert {item.outcome for item in audit} >= {"started", "failed"}


async def test_remediation_is_disabled_by_default():
    try:
        await remediation_service.execute("missing", "operator")
    except PermissionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("remediation executed outside lab")
