from fastapi.testclient import TestClient

from app.api import demo
from app.main import app


def test_demo_fault_proxy_routes(monkeypatch):
    """验证故障注入代理接口会转发到 demo-service 对应路径。"""
    calls = []

    async def fake_proxy(method, path, payload=None):
        """记录代理调用参数，避免测试依赖真实 demo-service。"""
        calls.append((method, path, payload))
        return {"path": path, "payload": payload}

    monkeypatch.setattr(demo, "_proxy_demo_request", fake_proxy)
    client = TestClient(app)

    cpu_response = client.post("/api/demo/faults/cpu", json={"enabled": True})
    clear_response = client.post("/api/demo/faults/clear")

    assert cpu_response.status_code == 200
    assert clear_response.status_code == 200
    assert calls == [
        ("POST", "/faults/cpu", {"enabled": True}),
        ("POST", "/faults/clear", None),
    ]
