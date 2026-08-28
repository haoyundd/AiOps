from app.config import config
from app.db import Incident, RemediationProposal, session_scope
from app.domain import IncidentStatus, ProposalStatus, RiskLevel
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
        proposal = RemediationProposal(
            incident_id=incident.id,
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


async def test_remediation_is_disabled_by_default():
    try:
        await remediation_service.execute("missing", "operator")
    except PermissionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("remediation executed outside lab")
