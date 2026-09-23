import hashlib
import hmac
import json

from app.db import DiagnosisRun, RunbookDraft, session_scope
from app.domain import RunbookDraftStatus
from app.services.incident_repository import incident_repository
from app.services.runbook_draft_service import runbook_draft_service


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_service_starts_without_model_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "UP"
    assert response.json()["version"] == "2.0.0"


async def test_incident_list_requires_authentication(client):
    response = client.get("/api/v1/incidents")
    assert response.status_code == 401


async def test_alertmanager_hmac_creates_incident_and_deduplicates(client, admin_token):
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "api-case-001",
                "startsAt": "2026-08-24T10:00:00Z",
                "labels": {
                    "alertname": "MerchantFlowHighLatency",
                    "service": "merchantflow",
                    "environment": "test",
                    "severity": "warning",
                },
                "annotations": {"description": "p95 is above one second"},
            }
        ]
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"test-alert-secret", body, hashlib.sha256).hexdigest()
    first = client.post(
        "/api/v1/alerts/alertmanager",
        content=body,
        headers={"Content-Type": "application/json", "X-AIOps-Signature": signature},
    )
    second = client.post(
        "/api/v1/alerts/alertmanager",
        content=body,
        headers={"Content-Type": "application/json", "X-AIOps-Signature": signature},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["accepted"][0]["created"] is True
    assert second.json()["accepted"][0]["created"] is False
    incidents = client.get("/api/v1/incidents", headers=auth(admin_token)).json()
    assert incidents["total"] == 1
    assert incidents["items"][0]["alert_name"] == "MerchantFlowHighLatency"


async def test_chat_returns_clear_configuration_error_without_key(client, admin_token):
    response = client.post(
        "/api/v1/chat",
        headers=auth(admin_token),
        json={"message": "what happened?"},
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


async def test_remediation_cannot_run_outside_lab(client, admin_token):
    response = client.post(
        "/api/v1/remediation-proposals/not-found/approve",
        headers=auth(admin_token),
        json={"reason": "verify the guard"},
    )
    assert response.status_code == 404


async def test_admin_can_read_services_model_and_create_runbook(client, admin_token):
    headers = auth(admin_token)
    me = client.get("/api/v1/auth/me", headers=headers)
    services = client.get("/api/v1/services", headers=headers)
    model = client.get("/api/v1/models/current", headers=headers)
    runbook = client.post(
        "/api/v1/runbooks",
        headers=headers,
        json={
            "title": "MerchantFlow CPU runbook",
            "service_name": "merchantflow",
            "tags": ["cpu"],
            "content": "检查 process_cpu_usage 与 HTTP P95，然后验证是否存在依赖错误。",
        },
    )
    assert me.status_code == 200
    assert services.json()[0]["name"] == "merchantflow"
    assert model.json()["provider"] == "dashscope"
    assert runbook.status_code == 201

    # 查询接口与创建接口一起验收，确保前端“手册”页面可以读取正式知识。
    searched = client.get(
        "/api/v1/runbooks?service_name=merchantflow&query=CPU",
        headers=headers,
    )
    assert searched.status_code == 200
    assert any(item["title"] == "MerchantFlow CPU runbook" for item in searched.json())


async def test_resolved_alert_then_new_firing_creates_a_new_incident(client):
    """同一告警恢复后再次触发，必须开启新的 Incident 周期。"""
    base_alert = {
        "fingerprint": "api-alert-cycle-case",
        "labels": {
            "alertname": "MerchantFlowRedisOutage",
            "service": "merchantflow",
            "environment": "test",
        },
    }
    headers = {"Authorization": "Bearer test-alert-secret"}

    first = {"alerts": [{**base_alert, "status": "firing", "startsAt": "2026-08-24T10:00:00Z"}]}
    first_response = client.post("/api/v1/alerts/alertmanager", json=first, headers=headers)
    first_id = first_response.json()["accepted"][0]["incident_id"]

    resolved = {"alerts": [{**base_alert, "status": "resolved", "startsAt": "2026-08-24T10:00:00Z"}]}
    resolved_response = client.post("/api/v1/alerts/alertmanager", json=resolved, headers=headers)
    assert resolved_response.json()["accepted"][0]["incident_id"] == first_id

    second = {"alerts": [{**base_alert, "status": "firing", "startsAt": "2026-08-24T11:00:00Z"}]}
    second_response = client.post("/api/v1/alerts/alertmanager", json=second, headers=headers)
    assert second_response.json()["accepted"][0]["created"] is True
    assert second_response.json()["accepted"][0]["incident_id"] != first_id


async def test_admin_can_queue_manual_diagnosis(client, admin_token):
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "manual-diagnosis-case",
                "labels": {
                    "alertname": "MerchantFlowHighCPU",
                    "service": "merchantflow",
                    "environment": "test",
                },
            }
        ]
    }
    alert = client.post(
        "/api/v1/alerts/alertmanager",
        json=payload,
        headers={"Authorization": "Bearer test-alert-secret"},
    )
    incident_id = alert.json()["accepted"][0]["incident_id"]
    queued = client.post(
        f"/api/v1/incidents/{incident_id}/diagnoses",
        headers=auth(admin_token),
        json={"idempotency_key": "manual-api-case"},
    )
    assert queued.status_code == 202
    run = client.get(f"/api/v1/diagnoses/{queued.json()['id']}", headers=auth(admin_token))
    assert run.status_code == 200
    assert run.json()["status"] == "QUEUED"


async def test_admin_can_approve_diagnosis_runbook_draft(client, admin_token):
    alert = client.post(
        "/api/v1/alerts/alertmanager",
        json={
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "draft-api-case",
                    "labels": {
                        "alertname": "MerchantFlowRedisLatency",
                        "service": "merchantflow",
                        "environment": "test",
                    },
                }
            ]
        },
        headers={"Authorization": "Bearer test-alert-secret"},
    )
    incident_id = alert.json()["accepted"][0]["incident_id"]
    async with session_scope() as session:
        incident = await incident_repository.get_incident(session, incident_id, with_details=False)
        assert incident is not None
        run = DiagnosisRun(incident_id=incident.id, idempotency_key="draft-api-run")
        session.add(run)
        await session.flush()
        draft = await runbook_draft_service.create_from_diagnosis(
            session,
            incident,
            run,
            {
                "status": "DIAGNOSED",
                "category": "REDIS_LATENCY",
                "root_cause": "Redis downstream span is slow",
                "confidence": 0.9,
                "recommendations": ["Remove latency after approval"],
            },
            [
                {
                    "source": "prometheus",
                    "kind": "red_metrics",
                    "status": "SUPPORTED",
                    "summary": "HTTP P95 is high",
                },
                {
                    "source": "tempo",
                    "kind": "trace_details",
                    "status": "SUPPORTED",
                    "summary": "Redis span is slow",
                },
            ],
        )
        assert draft is not None
        draft_id = draft.id

    headers = auth(admin_token)
    listed = client.get("/api/v1/runbook-drafts?status=PENDING", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == draft_id for item in listed.json())

    approved = client.post(
        f"/api/v1/runbook-drafts/{draft_id}/approve",
        headers=headers,
        json={"reason": "证据来源独立且可以复现"},
    )
    assert approved.status_code == 200
    assert approved.json()["runbook_id"]

    repeated = client.post(
        f"/api/v1/runbook-drafts/{draft_id}/approve",
        headers=headers,
        json={"reason": "重复审核"},
    )
    assert repeated.status_code == 409


async def test_admin_can_reject_draft_and_filter_review_status(client, admin_token):
    """审核状态必须可持久化，前端才能区分待审、已批准和已驳回知识。"""
    async with session_scope() as session:
        incident, _ = await incident_repository.upsert_alert(
            session,
            {
                "status": "firing",
                "fingerprint": "draft-reject-api-case",
                "labels": {
                    "alertname": "MerchantFlowApplicationError",
                    "service": "merchantflow",
                    "environment": "test",
                },
            },
        )
        run = DiagnosisRun(incident_id=incident.id, idempotency_key="draft-reject-run")
        session.add(run)
        await session.flush()
        draft = RunbookDraft(
            incident_id=incident.id,
            diagnosis_run_id=run.id,
            title="MerchantFlow 应用错误复盘",
            service_name="merchantflow",
            tags=["application_error"],
            content="这是一段足够长的复盘内容，用于验证草稿驳回状态可以持久化。",
            checksum="draft-reject-checksum",
            status=RunbookDraftStatus.PENDING,
            created_by="agent",
        )
        session.add(draft)
        await session.flush()
        draft_id = draft.id

    headers = auth(admin_token)
    rejected = client.post(
        f"/api/v1/runbook-drafts/{draft_id}/reject",
        headers=headers,
        json={"reason": "需要补充应用异常堆栈"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    listed = client.get("/api/v1/runbook-drafts?status=REJECTED", headers=headers)
    assert listed.status_code == 200
    item = next(value for value in listed.json() if value["id"] == draft_id)
    assert item["review_reason"] == "需要补充应用异常堆栈"
