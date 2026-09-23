from app.db import DiagnosisRun, session_scope
from app.domain import DiagnosisStatus
from app.services.incident_repository import incident_repository
from app.services.runbook_draft_service import (
    diagnosis_has_sufficient_evidence,
    runbook_draft_service,
)


def conclusion(status: str = "DIAGNOSED", confidence: float = 0.9) -> dict:
    return {
        "status": status,
        "category": "REDIS_LATENCY",
        "root_cause": "Redis downstream span is slow",
        "confidence": confidence,
        "recommendations": ["Remove the lab latency after approval"],
    }


def evidence() -> list[dict]:
    return [
        {
            "id": "evidence-metric",
            "source": "prometheus",
            "kind": "red_metrics",
            "status": "SUPPORTED",
            "summary": "HTTP P95 is 2.1s",
            "query": "latency_p95_seconds",
            "data": {"latency_p95_seconds": 2.1},
        },
        {
            "id": "evidence-trace",
            "source": "tempo",
            "kind": "trace_details",
            "status": "SUPPORTED",
            "summary": "Redis child span is 2.0s",
            "query": "trace-001",
            "data": {"spans": [{"service_name": "merchantflow", "duration_ms": 2000}]},
        },
    ]


async def test_draft_requires_diagnosed_and_two_independent_sources():
    assert diagnosis_has_sufficient_evidence(conclusion(), evidence()) is True
    assert diagnosis_has_sufficient_evidence(conclusion("INCONCLUSIVE"), evidence()) is False
    assert diagnosis_has_sufficient_evidence(conclusion(confidence=0.5), evidence()) is False
    assert diagnosis_has_sufficient_evidence(conclusion(), [evidence()[0]]) is False


def test_draft_counts_independent_evidence_kinds_not_duplicate_sources():
    """同一来源的不同观测类型可以构成独立证据，重复同类证据不能凑数。"""
    same_source_different_kinds = [
        {**evidence()[0], "kind": "jvm_process_metrics"},
        {**evidence()[0], "id": "evidence-red", "kind": "red_metrics"},
    ]
    same_kind_different_sources = [
        evidence()[0],
        {**evidence()[0], "id": "evidence-red-2", "source": "loki"},
    ]
    assert diagnosis_has_sufficient_evidence(conclusion(), same_source_different_kinds) is True
    assert diagnosis_has_sufficient_evidence(conclusion(), same_kind_different_sources) is False


async def test_draft_is_deterministically_deduplicated():
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(
            session,
            {
                "status": "firing",
                "fingerprint": "draft-service-case",
                "labels": {
                    "alertname": "MerchantFlowRedisLatency",
                    "service": "merchantflow",
                    "environment": "test",
                },
            },
        )
        run = DiagnosisRun(
            incident_id=incident.id,
            status=DiagnosisStatus.DIAGNOSED,
            idempotency_key="draft-run-001",
        )
        session.add(run)
        await session.flush()
        first = await runbook_draft_service.create_from_diagnosis(
            session, incident, run, conclusion(), evidence()
        )
        second = await runbook_draft_service.create_from_diagnosis(
            session, incident, run, conclusion(), evidence()
        )
        assert first is not None
        assert first.id == second.id
        assert first.status.value == "PENDING"
