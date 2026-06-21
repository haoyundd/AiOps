from app.agent.aiops.evaluation import evaluate_agent_run


SAFE_REPORT = "\n".join(
    [
        "# 诊断报告",
        "## 证据链",
        "- CPU 指标 demo_cpu_load 已超过阈值。",
        "- 服务健康检查正常。",
        "- 未发现 error/exception/timeout/failed 日志证据。",
        "- 发现 cpu spike injected warning 故障线索。",
        "## 结论",
        "当前证据支持 CPU 故障注入导致负载升高；错误日志检查只能说明当前窗口未命中 error 级别日志。",
    ]
)


def _run(final_report=SAFE_REPORT):
    """构造真实 AgentRun 摘要，避免单元测试依赖数据库。"""
    return {
        "id": "run-001",
        "incident_id": "incident-001",
        "status": "completed",
        "final_report": final_report,
    }


def _timeline(include_fault_signal=True):
    """构造真实 timeline，覆盖 Planner 计划和 Executor 工具证据。"""
    tool_events = [
        _tool_event("query_metric_summary", "demo_cpu_load max=0.95 exceeded=True evidence_type=metric_summary"),
        _tool_event("get_service_health", "up{service=\"demo-service\"}=1 service_health ok"),
        _tool_event("find_error_patterns", "返回条目数: 0，未发现 error/exception/timeout/failed 日志证据 error_patterns"),
        _tool_event("query_metric_summary", "demo_average_latency_seconds max=0.05 demo_error_total last=0"),
        _tool_event("propose_remediation", "requires_approval=True action=clear_faults"),
    ]
    if include_fault_signal:
        tool_events.insert(
            3,
            _tool_event("find_fault_signals", "WARNING cpu spike injected cpu_load=high fault_signals total_signal_samples=1"),
        )

    return [
        {
            "type": "plan_created",
            "message": "Planner 生成排查计划",
            "payload": {
                "planner_trace": {
                    "input_preview": "HighCPUUsage demo-service demo_cpu_load threshold=0.8",
                    "plan_steps": [
                        "调用 query_metric_summary 查询 demo_cpu_load 指标摘要",
                        "调用 get_service_health 检查服务健康",
                        "调用 find_error_patterns 检查错误日志",
                        "调用 find_fault_signals 检查 warning/cpu/fault 线索",
                        "调用 propose_remediation 生成需要人工确认的修复建议",
                    ],
                }
            },
        },
        *tool_events,
    ]


def _tool_event(name, preview):
    """构造一次工具执行事件，贴近 Executor 当前持久化 payload。"""
    return {
        "type": "tool_executed",
        "message": name,
        "payload": {
            "execution_event": {
                "step": f"执行 {name}",
                "tool_calls": [{"name": name, "args": {"service_name": "demo-service"}}],
                "tool_results": [{"content_preview": preview}],
                "result_preview": preview,
            }
        },
    }


def test_evaluate_agent_run_passes_with_complete_timeline():
    """真实 AgentRun 的计划、证据和报告都完整时，审计应通过。"""
    result = evaluate_agent_run(_run(), _timeline())

    assert result.case_id == "agent_run:run-001"
    assert result.expected_pass is True
    assert result.passed is True
    assert result.score == 100


def test_evaluate_agent_run_detects_missing_fault_signal():
    """真实 Run 缺少故障线索工具证据时，Evidence Gate 应指出 fault_signal_log 缺失。"""
    result = evaluate_agent_run(_run(), _timeline(include_fault_signal=False))
    evidence_check = next(check for check in result.checks if check.name == "evidence_sufficiency")

    assert result.passed is False
    assert "fault_signal_log" in evidence_check.details["missing_evidence_types"]


def test_evaluate_agent_run_detects_dangerous_report_phrase():
    """真实 Run 报告出现危险排除结论时，报告安全检查应失败。"""
    bad_report = SAFE_REPORT + "\n因此排除应用层问题。"

    result = evaluate_agent_run(_run(final_report=bad_report), _timeline())
    report_check = next(check for check in result.checks if check.name == "report_safety")

    assert result.passed is False
    assert "排除应用层问题" in report_check.details["matched_phrases"]
