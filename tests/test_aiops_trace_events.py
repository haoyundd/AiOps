from types import SimpleNamespace

from app.agent.aiops.evidence_gate import check_evidence_sufficiency
from app.agent.aiops.executor import _summarize_tool_calls, _summarize_tool_messages
from app.agent.aiops.planner import _build_alert_playbook_steps
from app.agent.aiops.replanner import _build_replan_event
from app.services.aiops_service import aiops_service


def test_planner_event_contains_trace_payload():
    """Planner 事件必须携带知识库、工具和计划信息，方便前端展示流程。"""
    event = aiops_service._format_planner_event(
        {
            "plan": ["查询 Prometheus 指标", "查询 Loki 日志"],
            "planner_trace": {
                "knowledge_hit": True,
                "knowledge_chars": 120,
                "local_tool_count": 2,
                "mcp_tool_count": 3,
                "tool_names": ["retrieve_knowledge", "query_metric_range"],
            },
        }
    )

    assert event["type"] == "plan"
    assert event["stage"] == "plan_created"
    assert event["planner_trace"]["knowledge_hit"] is True
    assert event["planner_trace"]["mcp_tool_count"] == 3


def test_executor_event_contains_tool_trace_payload():
    """Executor 事件必须暴露工具调用、参数和结果摘要。"""
    event = aiops_service._format_executor_event(
        {
            "plan": ["继续分析"],
            "past_steps": [("查询 CPU 指标", "CPU 已超过阈值")],
            "execution_events": [
                {
                    "step": "查询 CPU 指标",
                    "status": "success",
                    "duration_ms": 120,
                    "tool_calls": [{"name": "query_metric_range", "args": {"metric": "demo_cpu_load"}}],
                    "tool_results": [{"name": "query_metric_range", "content_preview": "cpu=0.95"}],
                }
            ],
        }
    )

    assert event["type"] == "step_complete"
    assert event["tool_call_count"] == 1
    assert event["execution_event"]["tool_calls"][0]["name"] == "query_metric_range"


def test_replanner_event_contains_decision_reason():
    """Replanner 决策事件必须解释为什么继续、重排或生成报告。"""
    replan_event = _build_replan_event(
        action="respond",
        reason="指标和日志证据已足够",
        plan=[],
        past_steps=[("查询指标", "完成"), ("查询日志", "完成")],
    )

    assert replan_event["action"] == "respond"
    assert replan_event["reason"] == "指标和日志证据已足够"
    assert replan_event["executed_steps"] == 2


def test_tool_summary_helpers_keep_frontend_payload_small():
    """工具调用和返回内容需要压缩成摘要，避免 incident payload 过大。"""
    calls = _summarize_tool_calls(
        [
            {"name": "query_service_logs", "args": {"service": "demo-service"}, "id": "call-1"},
        ]
    )
    messages = _summarize_tool_messages(
        [
            SimpleNamespace(
                name="query_service_logs",
                tool_call_id="call-1",
                content="x" * 1200,
            )
        ]
    )

    assert calls[0]["name"] == "query_service_logs"
    assert calls[0]["args"]["service"] == "demo-service"
    assert messages[0]["content_chars"] == 1200
    assert len(messages[0]["content_preview"]) == 1000


def test_high_cpu_playbook_contains_required_evidence_steps():
    """HighCPUUsage Playbook 必须固定包含核心证据链，避免模型漏查。"""
    steps = _build_alert_playbook_steps(
        "\n".join(
            [
                "请诊断以下真实 AIOps 告警",
                "- 服务名称: demo-service",
                "- 告警名称: HighCPUUsage",
                "- 指标名称: demo_cpu_load",
                "- 阈值: 0.8",
                "- 开始时间: 2026-06-02T10:00:00Z",
            ]
        )
    )

    joined = "\n".join(steps)
    assert "query_metric_summary" in joined
    assert "get_service_health" in joined
    assert "find_error_patterns" in joined
    assert "find_fault_signals" in joined
    assert "propose_remediation" in joined


def test_evidence_gate_blocks_high_cpu_without_fault_signals():
    """CPU 场景只查错误日志还不够，必须补齐故障线索日志。"""
    result = check_evidence_sufficiency(
        {
            "input": "告警名称: HighCPUUsage\n指标名称: demo_cpu_load",
            "past_steps": [
                ("query_metric_summary", "demo_cpu_load max=0.95 exceeded=True"),
                ("find_error_patterns", "返回条目数: 0"),
            ],
        }
    )

    assert result.passed is False
    assert "fault_signal_log" in result.missing_evidence_types


def test_evidence_gate_passes_high_cpu_with_core_evidence():
    """CPU 场景核心证据齐全后才允许生成确定性报告。"""
    result = check_evidence_sufficiency(
        {
            "input": "告警名称: HighCPUUsage\n指标名称: demo_cpu_load",
            "past_steps": [
                ("query_metric_summary", "demo_cpu_load max=0.95 exceeded=True"),
                ("get_service_health", "up{service=\"demo-service\"}=1"),
                ("find_error_patterns", "返回条目数: 0"),
                ("find_fault_signals", "cpu spike injected cpu_load=high"),
            ],
        }
    )

    assert result.passed is True
    assert result.missing_evidence_types == []
