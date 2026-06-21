from fastapi.testclient import TestClient

from app.api import alerts
from app.main import app


class _FakeIncidentCommandService:
    """模拟 IncidentService，避免 API 测试依赖真实数据库。"""

    def __init__(self):
        """初始化测试服务。"""
        self.received = None

    def get_command_panel(self, incident_id):
        """返回固定指挥面板。"""
        if incident_id == "missing":
            return None
        return {
            "incident_id": incident_id,
            "command_state": "detected",
            "summary": {"action_count": 0},
            "actions": [],
            "allowed_actions": ["acknowledge", "assign"],
        }

    def append_command_action(self, incident_id, action_type, actor, note, assignee, severity, payload):
        """记录 API 传入参数并返回动作结果。"""
        if incident_id == "missing":
            raise ValueError("incident 不存在")
        self.received = {
            "incident_id": incident_id,
            "action_type": action_type,
            "actor": actor,
            "note": note,
            "assignee": assignee,
            "severity": severity,
            "payload": payload,
        }
        return {
            "action": {"action_type": action_type, "assignee": assignee},
            "command": {"command_state": "assigned"},
        }


def test_incident_command_panel_api(monkeypatch):
    """指挥面板 API 应返回 command_state、允许动作和动作流。"""
    fake_service = _FakeIncidentCommandService()
    monkeypatch.setattr(alerts, "incident_service", fake_service)
    client = TestClient(app)

    response = client.get("/api/incidents/case-001/command")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["incident_id"] == "case-001"
    assert data["command_state"] == "detected"
    assert "assign" in data["allowed_actions"]


def test_incident_command_action_api(monkeypatch):
    """提交指挥动作 API 应把请求字段完整传给服务层。"""
    fake_service = _FakeIncidentCommandService()
    monkeypatch.setattr(alerts, "incident_service", fake_service)
    client = TestClient(app)

    response = client.post(
        "/api/incidents/case-001/command/actions",
        json={
            "action_type": "assign",
            "actor": "oncall",
            "note": "交给后端处理",
            "assignee": "backend",
            "payload": {"channel": "war-room"},
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["command"]["command_state"] == "assigned"
    assert fake_service.received["assignee"] == "backend"
    assert fake_service.received["payload"]["channel"] == "war-room"


def test_incident_command_action_api_returns_404(monkeypatch):
    """不存在的 incident 应返回 404，而不是吞掉错误。"""
    fake_service = _FakeIncidentCommandService()
    monkeypatch.setattr(alerts, "incident_service", fake_service)
    client = TestClient(app)

    response = client.post(
        "/api/incidents/missing/command/actions",
        json={"action_type": "acknowledge", "actor": "oncall"},
    )

    assert response.status_code == 404
