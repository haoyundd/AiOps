from fastapi.testclient import TestClient

from app.api import alerts, evaluations
from app.main import app
from tests.test_agent_run_evaluation import SAFE_REPORT, _timeline


class _FakeRunEvaluationRepository:
    """模拟真实 Run 仓储，专门验证 API 编排和持久化调用。"""

    def __init__(self, run=None, timeline=None):
        """初始化 fake 数据，默认给出一个已完成且有报告的 Run。"""
        self.run = run or {
            "id": "run-001",
            "incident_id": "incident-001",
            "status": "completed",
            "final_report": SAFE_REPORT,
        }
        self.timeline = timeline if timeline is not None else _timeline()
        self.evaluations = []
        self.timeline_appends = []

    def get_run(self, run_id):
        """按 run_id 查询 Run。"""
        if self.run and self.run["id"] == run_id:
            return self.run
        return None

    def list_timeline_for_run(self, run_id):
        """返回该 Run 的 timeline。"""
        return self.timeline if self.get_run(run_id) else []

    def get_state(self, run_id):
        """API 兼容：返回最新状态快照。"""
        return {"run_id": run_id, "state": {"event_type": "complete"}}

    def save_run_evaluation(self, run_id, result):
        """保存审计结果，并返回 API 可序列化记录。"""
        record = {
            "id": f"eval-{len(self.evaluations) + 1}",
            "run_id": run_id,
            "passed": result["passed"],
            "score": result["score"],
            "result": result,
            "created_at": "2026-06-21T10:00:00+00:00",
        }
        self.evaluations.append(record)
        return record

    def list_evaluations_for_run(self, run_id):
        """查询该 Run 的审计历史。"""
        return [record for record in self.evaluations if record["run_id"] == run_id]

    def append_timeline(self, incident_id, event_type, message, payload=None, run_id=None):
        """记录 API 是否写入 evaluation_completed timeline。"""
        self.timeline_appends.append(
            {
                "incident_id": incident_id,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "run_id": run_id,
            }
        )


class _FakeRunEvaluationService:
    """模拟 IncidentService，只暴露 API 所需 repository。"""

    def __init__(self, repository):
        """注入 fake repository。"""
        self.repository = repository


def test_evaluate_agent_run_api_persists_and_get_returns_history(monkeypatch):
    """真实 Run 审计 API 应保存结果，并能在 AgentRun 查询接口中回查。"""
    repository = _FakeRunEvaluationRepository()
    service = _FakeRunEvaluationService(repository)
    monkeypatch.setattr(evaluations, "incident_service", service)
    monkeypatch.setattr(alerts, "incident_service", service)
    client = TestClient(app)

    response = client.post("/api/evaluations/agent-runs/run-001")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["evaluation"]["passed"] is True
    assert data["evaluation"]["result"]["case_id"] == "agent_run:run-001"
    assert repository.timeline_appends[-1]["event_type"] == "evaluation_completed"

    get_response = client.get("/api/agent-runs/run-001")
    get_data = get_response.json()["data"]
    assert get_response.status_code == 200
    assert get_data["evaluations"][0]["id"] == data["evaluation"]["id"]


def test_evaluate_agent_run_api_returns_404_for_missing_run(monkeypatch):
    """不存在的 Run 应返回 404，而不是生成空审计。"""
    repository = _FakeRunEvaluationRepository(run=None)
    monkeypatch.setattr(evaluations, "incident_service", _FakeRunEvaluationService(repository))
    client = TestClient(app)

    response = client.post("/api/evaluations/agent-runs/missing")

    assert response.status_code == 404


def test_evaluate_agent_run_api_rejects_unfinished_run(monkeypatch):
    """未完成的 Run 不应被审计，避免把中间态误判为质量失败。"""
    repository = _FakeRunEvaluationRepository(
        run={"id": "run-001", "incident_id": "incident-001", "status": "running", "final_report": SAFE_REPORT}
    )
    monkeypatch.setattr(evaluations, "incident_service", _FakeRunEvaluationService(repository))
    client = TestClient(app)

    response = client.post("/api/evaluations/agent-runs/run-001")

    assert response.status_code == 400
    assert "尚未完成" in response.json()["detail"]


def test_evaluate_agent_run_api_rejects_run_without_report(monkeypatch):
    """缺少最终报告的 Run 不能执行报告安全审计。"""
    repository = _FakeRunEvaluationRepository(
        run={"id": "run-001", "incident_id": "incident-001", "status": "completed", "final_report": ""}
    )
    monkeypatch.setattr(evaluations, "incident_service", _FakeRunEvaluationService(repository))
    client = TestClient(app)

    response = client.post("/api/evaluations/agent-runs/run-001")

    assert response.status_code == 400
    assert "缺少最终报告" in response.json()["detail"]
