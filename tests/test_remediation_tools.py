from mcp_servers import remediation_server


def test_remediation_requires_approval():
    """没有用户确认时，修复工具必须拒绝执行。"""
    result = remediation_server._execute_approved_remediation_impl(
        action="clear_faults",
        service_name="demo-service",
        reason="测试未确认保护",
        approved=False,
    )

    assert result["type"] == "remediation_blocked"
    assert result["success"] is False
    assert "未获得用户确认" in result["message"]


def test_remediation_rejects_non_whitelisted_action():
    """非白名单动作即使 approved=True 也不能执行。"""
    result = remediation_server._execute_approved_remediation_impl(
        action="run_shell",
        service_name="demo-service",
        reason="测试白名单保护",
        approved=True,
    )

    assert result["type"] == "remediation_blocked"
    assert result["success"] is False
    assert "白名单" in result["message"]


def test_remediation_executes_allowed_action_after_approval(monkeypatch):
    """白名单动作在用户确认后才会调用 demo service。"""

    def fake_call(method, path, json=None):
        return {"method": method, "path": path, "json": json}

    monkeypatch.setattr(remediation_server, "_call_demo_service", fake_call)

    result = remediation_server._execute_approved_remediation_impl(
        action="set_cpu_spike",
        service_name="demo-service",
        reason="CPU 故障已确认需要关闭",
        approved=True,
        parameters={"enabled": False},
    )

    assert result["type"] == "remediation_executed"
    assert result["success"] is True
    assert result["result"]["path"] == "/faults/cpu"
    assert result["result"]["json"] == {"enabled": False}
