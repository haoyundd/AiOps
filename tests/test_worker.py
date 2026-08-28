
from sqlalchemy import select

from app.db import DiagnosisRun, EvidenceItem, HypothesisRecord, Incident, session_scope
from app.domain import DiagnosisStatus, IncidentStatus
from app.services.incident_repository import incident_repository
from app.worker import DiagnosisWorker


def alert(fingerprint: str) -> dict:
    return {
        "status": "firing",
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "MerchantFlowHighCPU",
            "service": "merchantflow",
            "environment": "test",
        },
    }


def graph_result() -> dict:
    evidence_id = "evidence-001"
    return {
        "conclusion": {
            "status": "DIAGNOSED",
            "root_cause": "MerchantFlow process CPU is saturated",
            "category": "CPU_SATURATION",
            "confidence": 0.9,
            "summary": "CPU evidence is sufficient",
            "report_markdown": "# CPU saturation",
        },
        "evidence": [
            {
                "id": evidence_id,
                "hypothesis_id": None,
                "source": "prometheus",
                "kind": "jvm_process_metrics",
                "status": "SUPPORTED",
                "summary": "process_cpu_usage=0.93",
                "query": "process_cpu_usage",
                "data": {"process_cpu_usage": 0.93},
            }
        ],
        "hypotheses": [
            {
                "id": "hypothesis-001",
                "rank": 1,
                "category": "CPU_SATURATION",
                "title": "CPU is saturated",
                "confidence": 0.9,
                "verdict": "SUPPORTED",
                "supporting_evidence_ids": [evidence_id],
                "contradicting_evidence_ids": [],
            }
        ],
        "tool_calls": [
            {
                "id": "tool-001",
                "tool_name": "get_jvm_metrics",
                "source": "prometheus",
                "risk_level": "READ_ONLY",
                "status": "success",
                "input": {"service_name": "merchantflow"},
                "output": {"process_cpu_usage": 0.93},
                "error": "",
                "duration_ms": 10,
            }
        ],
    }


async def queue_case(fingerprint: str) -> tuple[str, str]:
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(session, alert(fingerprint))
        run, _ = await incident_repository.queue_diagnosis(
            session,
            incident,
            trigger="manual",
            requested_by="operator",
            idempotency_key=f"manual:{fingerprint}",
        )
        return incident.id, run.id


async def test_worker_claims_persists_and_completes_diagnosis(monkeypatch):
    incident_id, run_id = await queue_case("worker-success")

    async def fake_run(**_kwargs):
        return graph_result()

    monkeypatch.setattr("app.worker.evidence_diagnosis_graph.run", fake_run)
    assert await DiagnosisWorker("test-worker").run_once() is True
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.get(DiagnosisRun, run_id)
        assert incident.status == IncidentStatus.DIAGNOSED
        assert run.status == DiagnosisStatus.DIAGNOSED
        assert run.conclusion["category"] == "CPU_SATURATION"
        assert await session.scalar(select(EvidenceItem).where(EvidenceItem.diagnosis_run_id == run_id))
        assert await session.scalar(
            select(HypothesisRecord).where(HypothesisRecord.diagnosis_run_id == run_id)
        )


async def test_worker_marks_failed_when_graph_raises(monkeypatch):
    incident_id, run_id = await queue_case("worker-failure")

    async def failed_run(**_kwargs):
        raise RuntimeError("Tempo query parser failed")

    monkeypatch.setattr("app.worker.evidence_diagnosis_graph.run", failed_run)
    assert await DiagnosisWorker("test-worker").run_once() is True
    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.get(DiagnosisRun, run_id)
        assert incident.status == IncidentStatus.FAILED
        assert run.status == DiagnosisStatus.FAILED
        assert "Tempo query parser failed" in run.error


async def test_idle_worker_returns_false():
    assert await DiagnosisWorker("idle-worker").run_once() is False
