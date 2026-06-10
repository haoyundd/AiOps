from app.config import config
from app.models.aiops import AIOpsRequest
import app.services.incident_service as incident_module
from app.services.incident_service import IncidentService


def test_alertmanager_alert_creates_incident_without_running_agent_when_disabled(monkeypatch):
    """关闭自动诊断时，告警只创建 incident，不自动调用大模型诊断。"""
    service = IncidentService()
    started = []
    monkeypatch.setattr(config, "aiops_auto_diagnosis_enabled", False)

    def fake_start_diagnosis(incident_id, request):
        started.append((incident_id, request))

    monkeypatch.setattr(service, "start_diagnosis", fake_start_diagnosis)

    incident = service.create_or_update_from_alert(
        {
            "status": "firing",
            "fingerprint": "case-001",
            "startsAt": "2026-06-02T10:00:00Z",
            "labels": {
                "alertname": "HighCPUUsage",
                "service": "demo-service",
                "severity": "critical",
                "metric": "demo_cpu_load",
                "threshold": "0.8",
                "environment": "local",
            },
            "annotations": {
                "description": "CPU load is too high",
            },
        }
    )

    assert incident["id"] == "case-001"
    assert incident["status"] == "new"
    assert incident["service_name"] == "demo-service"
    assert incident["metric_name"] == "demo_cpu_load"
    assert incident["threshold"] == 0.8
    assert started == []
    assert incident["timeline"][-1]["type"] == "diagnosis_waiting_confirmation"


def test_alertmanager_alert_can_auto_start_agent_when_enabled(monkeypatch):
    """明确开启自动诊断后，firing 告警才会启动后台 Agent。"""
    service = IncidentService()
    started = []
    monkeypatch.setattr(config, "aiops_auto_diagnosis_enabled", True)

    def fake_start_diagnosis(incident_id, request):
        started.append((incident_id, request))

    monkeypatch.setattr(service, "start_diagnosis", fake_start_diagnosis)

    incident = service.create_or_update_from_alert(
        {
            "status": "firing",
            "fingerprint": "case-002",
            "labels": {
                "alertname": "HighCPUUsage",
                "service": "demo-service",
                "metric": "demo_cpu_load",
            },
        }
    )

    assert incident["id"] == "case-002"
    assert started[0][1].alert_name == "HighCPUUsage"


async def test_diagnosis_error_marks_incident_failed(monkeypatch):
    """诊断流出现 error 时，incident 必须失败，不能被误标为 diagnosed。"""
    service = IncidentService()
    request = AIOpsRequest(session_id="case-error")
    service.start_manual_diagnosis(request)

    async def fake_diagnose(request, session_id):
        yield {
            "type": "error",
            "message": "模型结构化输出失败",
        }

    monkeypatch.setattr(incident_module.aiops_service, "diagnose", fake_diagnose)
    await service._run_diagnosis("case-error", request)

    incident = service.get_incident("case-error")
    assert incident["status"] == "failed"
    assert incident["timeline"][-1]["type"] == "diagnosis_failed"
