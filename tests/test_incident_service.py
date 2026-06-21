from app.config import config
from app.models.aiops import AIOpsRequest
from app.repositories.incident_repository import IncidentRepository
import app.services.incident_service as incident_module
from app.services.incident_service import IncidentService


def _repo(tmp_path):
    """创建测试专用 Repository，避免污染本地演示数据库。"""
    return IncidentRepository(database_url=f"sqlite:///{tmp_path / 'aiops_test.db'}")


def test_alertmanager_alert_creates_incident_without_running_agent_when_disabled(tmp_path, monkeypatch):
    """关闭自动诊断时，告警只创建 incident，不自动调用大模型诊断。"""
    service = IncidentService(repository=_repo(tmp_path))
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


def test_incident_persists_across_service_instances(tmp_path, monkeypatch):
    """incident 应持久化到 SQLite，新服务实例仍能查询历史 incident。"""
    repository = _repo(tmp_path)
    service = IncidentService(repository=repository)
    monkeypatch.setattr(config, "aiops_auto_diagnosis_enabled", False)

    service.create_or_update_from_alert(
        {
            "status": "firing",
            "fingerprint": "persist-001",
            "labels": {
                "alertname": "HighCPUUsage",
                "service": "demo-service",
                "metric": "demo_cpu_load",
            },
        }
    )

    reloaded_service = IncidentService(repository=_repo(tmp_path))
    incident = reloaded_service.get_incident("persist-001")

    assert incident is not None
    assert incident["id"] == "persist-001"
    assert incident["alert_name"] == "HighCPUUsage"
    assert incident["timeline"][-1]["type"] == "diagnosis_waiting_confirmation"


def test_agent_runs_are_isolated_for_same_incident(tmp_path):
    """同一个 incident 多次诊断应生成多个独立 AgentRun。"""
    repository = _repo(tmp_path)
    service = IncidentService(repository=repository)
    service.repository.upsert_incident(
        {
            "id": "run-case",
            "fingerprint": "run-case",
            "status": "new",
            "alert_status": "manual",
            "service_name": "demo-service",
            "alert_name": "HighCPUUsage",
            "severity": "warning",
            "metric_name": "demo_cpu_load",
            "threshold": 0.8,
            "environment": "local",
            "raw_alert": {},
            "report": "",
        }
    )

    first = service.repository.create_agent_run("run-case", "manual", "xiaomi", "mimo-v2-flash")
    second = service.repository.create_agent_run("run-case", "manual", "xiaomi", "mimo-v2-flash")
    runs = service.repository.list_runs_for_incident("run-case")

    assert first["id"] != second["id"]
    assert len(runs) == 2
    assert {run["id"] for run in runs} == {first["id"], second["id"]}


def test_incident_command_actions_drive_command_state(tmp_path):
    """ICM 动作流应能推导确认、分派、升级和缓解状态。"""
    service = IncidentService(repository=_repo(tmp_path))
    service.repository.upsert_incident(
        {
            "id": "icm-case",
            "fingerprint": "icm-case",
            "status": "new",
            "alert_status": "firing",
            "service_name": "demo-service",
            "alert_name": "HighCPUUsage",
            "severity": "warning",
            "metric_name": "demo_cpu_load",
            "threshold": 0.8,
            "environment": "local",
            "raw_alert": {},
            "report": "",
        }
    )

    service.append_command_action("icm-case", "acknowledge", actor="oncall", note="开始接手")
    service.append_command_action("icm-case", "assign", actor="oncall", assignee="backend", note="交给后端排查")
    service.append_command_action("icm-case", "escalate", actor="oncall", severity="critical", note="影响扩大")
    service.append_command_action("icm-case", "mitigate", actor="backend", note="临时缓解")

    panel = service.get_command_panel("icm-case")
    incident = service.get_incident("icm-case")

    assert panel["command_state"] == "mitigated"
    assert panel["summary"]["assignee"] == "backend"
    assert panel["summary"]["severity"] == "critical"
    assert len(panel["actions"]) == 4
    assert incident["timeline"][-1]["type"] == "command_mitigate"


def test_incident_command_rejects_invalid_transition_after_resolve(tmp_path):
    """resolved 状态只允许备注或重开，不能继续分派。"""
    service = IncidentService(repository=_repo(tmp_path))
    service.repository.upsert_incident(
        {
            "id": "resolved-case",
            "fingerprint": "resolved-case",
            "status": "new",
            "alert_status": "firing",
            "service_name": "demo-service",
            "alert_name": "HighCPUUsage",
            "severity": "warning",
            "metric_name": "demo_cpu_load",
            "environment": "local",
            "raw_alert": {},
            "report": "",
        }
    )

    service.append_command_action("resolved-case", "resolve", actor="oncall", note="故障恢复")

    try:
        service.append_command_action("resolved-case", "assign", actor="oncall", assignee="backend")
    except ValueError as e:
        assert "不允许执行动作 assign" in str(e)
    else:
        raise AssertionError("resolved 状态不应允许 assign")

    incident = service.get_incident("resolved-case")
    assert incident["status"] == "resolved"
    assert incident["alert_status"] == "resolved"
    assert incident["command_state"] == "resolved"


def test_incident_command_reopen_resolved_case(tmp_path):
    """reopen 应将已恢复 incident 重新打开，并进入 investigating 状态。"""
    service = IncidentService(repository=_repo(tmp_path))
    service.repository.upsert_incident(
        {
            "id": "reopen-case",
            "fingerprint": "reopen-case",
            "status": "resolved",
            "alert_status": "resolved",
            "service_name": "demo-service",
            "alert_name": "HighCPUUsage",
            "severity": "warning",
            "metric_name": "demo_cpu_load",
            "environment": "local",
            "raw_alert": {},
            "report": "",
        }
    )

    service.append_command_action("reopen-case", "reopen", actor="oncall", note="症状复现")

    incident = service.get_incident("reopen-case")
    assert incident["status"] == "new"
    assert incident["alert_status"] == "firing"
    assert incident["command_state"] == "investigating"
    assert incident["timeline"][-1]["type"] == "command_reopen"


def test_alertmanager_alert_can_auto_start_agent_when_enabled(tmp_path, monkeypatch):
    """明确开启自动诊断后，firing 告警才会启动后台 Agent。"""
    service = IncidentService(repository=_repo(tmp_path))
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


async def test_diagnosis_error_marks_incident_failed(tmp_path, monkeypatch):
    """诊断流出现 error 时，incident 必须失败，不能被误标为 diagnosed。"""
    service = IncidentService(repository=_repo(tmp_path))
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
