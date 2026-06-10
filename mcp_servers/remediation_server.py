"""Remediation MCP Server.

只提供白名单半自动修复动作。所有执行动作都要求 approved=True，避免 Agent
在没有人工确认时直接修改运行状态。
"""

import os
from typing import Any, Dict

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Remediation")

DEMO_SERVICE_URL = os.getenv("DEMO_SERVICE_URL", "http://localhost:9910").rstrip("/")
ALLOWED_ACTIONS = {
    "clear_faults",
    "set_cpu_spike",
    "set_slow_response",
    "set_error_mode",
    "health_check",
}


def _call_demo_service(method: str, path: str, json: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """调用 demo service 的受控接口。"""
    url = f"{DEMO_SERVICE_URL}{path}"
    with httpx.Client(timeout=10.0) as client:
        response = client.request(method, url, json=json)
        response.raise_for_status()
        payload = response.json()
    return {"url": url, "status_code": response.status_code, "payload": payload}


def _propose_remediation_impl(
    service_name: str,
    alert_name: str,
    root_cause: str,
) -> Dict[str, Any]:
    """根据根因生成待确认修复动作，不执行任何修改。"""
    cause = root_cause.lower()
    if "cpu" in cause:
        action = "set_cpu_spike"
        parameters = {"enabled": False}
    elif "slow" in cause or "latency" in cause or "timeout" in cause:
        action = "set_slow_response"
        parameters = {"enabled": False}
    elif "error" in cause or "exception" in cause or "failed" in cause:
        action = "set_error_mode"
        parameters = {"enabled": False}
    else:
        action = "health_check"
        parameters = {}

    return {
        "type": "remediation_proposed",
        "service_name": service_name,
        "alert_name": alert_name,
        "action": action,
        "parameters": parameters,
        "requires_approval": True,
        "reason": f"根据根因判断生成修复建议: {root_cause}",
        "rollback": "demo service 故障注入动作均可通过 clear_faults 还原。",
    }


@mcp.tool()
def propose_remediation(
    service_name: str,
    alert_name: str,
    root_cause: str,
) -> Dict[str, Any]:
    """根据根因生成待确认修复动作，不执行任何修改。"""
    return _propose_remediation_impl(service_name, alert_name, root_cause)


def _execute_approved_remediation_impl(
    action: str,
    service_name: str,
    reason: str,
    approved: bool = False,
    parameters: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """执行用户确认后的修复动作。"""
    if not approved:
        return {
            "type": "remediation_blocked",
            "success": False,
            "message": "修复动作未获得用户确认，已拒绝执行。",
            "action": action,
        }

    if service_name != "demo-service":
        return {
            "type": "remediation_blocked",
            "success": False,
            "message": "第一版只允许修复 demo-service。",
            "action": action,
            "service_name": service_name,
        }

    if action not in ALLOWED_ACTIONS:
        return {
            "type": "remediation_blocked",
            "success": False,
            "message": f"动作不在白名单中: {action}",
            "allowed_actions": sorted(ALLOWED_ACTIONS),
        }

    parameters = parameters or {}
    if action == "clear_faults":
        result = _call_demo_service("POST", "/faults/clear")
    elif action == "set_cpu_spike":
        result = _call_demo_service("POST", "/faults/cpu", {"enabled": bool(parameters.get("enabled"))})
    elif action == "set_slow_response":
        result = _call_demo_service("POST", "/faults/slow", {"enabled": bool(parameters.get("enabled"))})
    elif action == "set_error_mode":
        result = _call_demo_service("POST", "/faults/error", {"enabled": bool(parameters.get("enabled"))})
    else:
        result = _call_demo_service("GET", "/health")

    return {
        "type": "remediation_executed",
        "success": True,
        "action": action,
        "service_name": service_name,
        "reason": reason,
        "result": result,
        "rollback": "如需回滚，请执行 clear_faults 或重新打开对应故障注入开关。",
    }


@mcp.tool()
def execute_approved_remediation(
    action: str,
    service_name: str,
    reason: str,
    approved: bool = False,
    parameters: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """执行用户确认后的修复动作。"""
    return _execute_approved_remediation_impl(
        action=action,
        service_name=service_name,
        reason=reason,
        approved=approved,
        parameters=parameters,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8005, path="/mcp")
