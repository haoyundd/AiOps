import hashlib
import hmac
import json


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
