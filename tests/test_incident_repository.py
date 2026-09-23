from sqlalchemy import select

from app.db import Incident, session_scope
from app.domain import IncidentStatus
from app.services.incident_repository import incident_repository


def alert_payload(fingerprint: str = "case-001") -> dict:
    return {
        "status": "firing",
        "fingerprint": fingerprint,
        "startsAt": "2026-08-24T10:00:00Z",
        "labels": {
            "alertname": "MerchantFlowHighCPU",
            "service": "merchantflow",
            "severity": "critical",
            "metric": "process_cpu_usage",
            "threshold": "0.8",
            "environment": "test",
        },
        "annotations": {"description": "real process CPU is above threshold"},
    }


async def test_alert_upsert_is_durable_and_deduplicated():
    async with session_scope() as session:
        first, created = await incident_repository.upsert_alert(session, alert_payload())
        first_id = first.id
        assert created is True
    async with session_scope() as session:
        second, created = await incident_repository.upsert_alert(session, alert_payload())
        assert created is False
        assert second.id == first_id
        assert second.version == 2
    async with session_scope() as session:
        assert len((await session.scalars(select(Incident))).all()) == 1


async def test_resolved_alert_transitions_same_incident():
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(session, alert_payload("resolved-case"))
        incident_id = incident.id
    resolved = alert_payload("resolved-case")
    resolved["status"] = "resolved"
    async with session_scope() as session:
        incident, created = await incident_repository.upsert_alert(session, resolved)
        assert created is False
        assert incident.id == incident_id
        assert incident.status == IncidentStatus.RESOLVED
        assert incident.resolved_at is not None


async def test_new_firing_after_resolution_gets_new_incident():
    """同一告警规则的新 firing 不能复用上一轮已恢复 Incident。"""
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(
            session, alert_payload("cycle-case")
        )
        old_id = incident.id
        resolved = alert_payload("cycle-case")
        resolved["status"] = "resolved"
        await incident_repository.upsert_alert(session, resolved)

    next_cycle = alert_payload("cycle-case")
    next_cycle["startsAt"] = "2026-08-24T11:00:00Z"
    async with session_scope() as session:
        current, created = await incident_repository.upsert_alert(session, next_cycle)
        assert created is True
        assert current.id != old_id
        assert current.status == IncidentStatus.RECEIVED


async def test_diagnosis_idempotency_key_prevents_duplicate_jobs():
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(session, alert_payload("queue-case"))
        first, created = await incident_repository.queue_diagnosis(
            session,
            incident,
            trigger="alert",
            requested_by="alertmanager",
            idempotency_key="alert:queue-case:start",
        )
        second, duplicate_created = await incident_repository.queue_diagnosis(
            session,
            incident,
            trigger="alert",
            requested_by="alertmanager",
            idempotency_key="alert:queue-case:start",
        )
        assert created is True
        assert duplicate_created is False
        assert first.id == second.id
