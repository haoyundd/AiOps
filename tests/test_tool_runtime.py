from types import SimpleNamespace

import pytest

from app.agent.aiops.tool_runtime import (
    evaluate_tool_policy,
    execute_planned_tool_step,
    parse_planned_tool_call,
    precheck_tool_calls,
)


class _FakeTool:
    """模拟 LangChain Tool，记录运行时传入的参数。"""

    name = "query_metric_summary"

    def __init__(self):
        """初始化测试工具。"""
        self.called_with = None

    async def ainvoke(self, args):
        """返回可断言的工具结果。"""
        self.called_with = args
        return {"evidence_type": "metric_summary", "summary": {"max": 0.95}}


def test_parse_planned_tool_call_from_playbook_step():
    """运行时应能从中文 Playbook 步骤中解析显式工具和参数。"""
    call = parse_planned_tool_call(
        '使用 query_metric_summary(metric_name="demo_cpu_load", service_name="demo-service", threshold=0.8) 查询指标。'
    )

    assert call is not None
    assert call.name == "query_metric_summary"
    assert call.args == {
        "metric_name": "demo_cpu_load",
        "service_name": "demo-service",
        "threshold": 0.8,
    }


@pytest.mark.asyncio
async def test_execute_planned_tool_step_runs_readonly_tool():
    """观测类工具应通过策略校验，并被确定性运行时直接执行。"""
    fake_tool = _FakeTool()

    result = await execute_planned_tool_step(
        '使用 query_metric_summary(metric_name="demo_cpu_load", service_name="demo-service", threshold=0.8) 查询指标。',
        [fake_tool],
    )

    assert result is not None
    assert result.status == "success"
    assert result.policy_decision is not None
    assert result.policy_decision.risk_level == "low"
    assert fake_tool.called_with["service_name"] == "demo-service"
    assert "metric_summary" in result.result_text


def test_policy_blocks_agent_remediation_execution():
    """真实修复工具禁止由 Agent 自动执行，必须走人工确认入口。"""
    decision = evaluate_tool_policy(
        "execute_approved_remediation",
        {"action": "clear_faults", "service_name": "demo-service", "approved": True},
        execution_mode="agent",
    )

    assert decision.allowed is False
    assert decision.risk_level == "high"
    assert decision.requires_approval is True


def test_policy_allows_manual_approved_remediation():
    """人工确认入口传入 approved=True 时，策略允许调用修复 MCP。"""
    decision = evaluate_tool_policy(
        "execute_approved_remediation",
        {"action": "clear_faults", "service_name": "demo-service", "approved": True},
        execution_mode="manual_remediation",
    )

    assert decision.allowed is True
    assert decision.risk_level == "high"


def test_policy_blocks_placeholder_arguments():
    """占位参数会导致查错服务，应在工具执行前阻断。"""
    decision = evaluate_tool_policy(
        "query_metric_summary",
        {"metric_name": "demo_cpu_load", "service_name": "指定服务"},
        execution_mode="agent",
    )

    assert decision.allowed is False
    assert decision.risk_level == "invalid"
    assert "占位词" in decision.reason


def test_precheck_blocks_llm_high_risk_tool_call():
    """模型自主发起高风险工具调用时，ToolNode 执行前必须被拦截。"""
    result = precheck_tool_calls(
        [
            SimpleNamespace(
                name="execute_approved_remediation",
                args={"action": "clear_faults", "service_name": "demo-service", "approved": True},
                id="call-1",
            )
        ]
    )

    assert result is not None
    assert result.status == "blocked"
    assert result.policy_decision is not None
    assert result.policy_decision.risk_level == "high"
