from fastapi.testclient import TestClient

from app.main import app


def test_list_evaluation_cases_api():
    """评测场景 API 应返回内置回放用例摘要。"""
    client = TestClient(app)

    response = client.get("/api/evaluations/cases")

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 3
    assert data[0]["id"] == "high_cpu_complete"


def test_replay_evaluations_api():
    """回放评测 API 应执行内置套件，并返回通过情况和逐 case 结果。"""
    client = TestClient(app)

    response = client.post("/api/evaluations/replay")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["suite"] == "builtin-aiops-eval"
    assert data["passed"] is True
    assert data["case_count"] == 3
    assert len(data["results"]) == 3
