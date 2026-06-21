"""AIOps Evaluation Harness。

评测层不调用大模型，也不访问外部系统。它只检查一次诊断产物是否满足工程质量门禁：
计划是否覆盖必查工具、证据是否足够、报告是否出现危险结论。
"""

import json
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, Field

from .evidence_gate import check_evidence_sufficiency
from .planner import _build_alert_playbook_steps


HIGH_CPU_INPUT = "\n".join(
    [
        "请诊断以下真实 AIOps 告警，并输出带证据链的 Markdown 报告。",
        "- 服务名称: demo-service",
        "- 告警名称: HighCPUUsage",
        "- 告警级别: critical",
        "- 指标名称: demo_cpu_load",
        "- 阈值: 0.8",
        "- 开始时间: 2026-06-19T10:00:00Z",
        "- 环境: local",
        "- 描述: demo-service CPU 持续高于阈值",
    ]
)

HIGH_CPU_REQUIRED_TOOLS = [
    "query_metric_summary",
    "get_service_health",
    "find_error_patterns",
    "find_fault_signals",
    "propose_remediation",
]

FORBIDDEN_REPORT_PHRASES = [
    "排除应用层问题",
    "排除应用层错误",
    "因此可排除应用层",
    "因此排除应用层",
    "确认不是应用层问题",
    "不是应用层导致",
]


class AIOpsEvalCase(BaseModel):
    """一次固定或真实 Run 回放评测场景。"""

    id: str = Field(description="评测场景 ID")
    name: str = Field(description="评测场景名称")
    input_text: str = Field(description="原始告警输入")
    plan: List[str] = Field(description="待评测计划步骤")
    past_steps: List[Tuple[str, str]] = Field(description="待评测证据步骤")
    report: str = Field(description="待评测最终报告")
    expected_pass: bool = Field(description="该场景期望是否通过")


class AIOpsEvalCheck(BaseModel):
    """单个评测检查项。"""

    name: str
    passed: bool
    reason: str
    severity: str = "info"
    details: Dict[str, Any] = Field(default_factory=dict)


class AIOpsEvalResult(BaseModel):
    """评测结果。"""

    case_id: str
    passed: bool
    expected_pass: bool
    score: int
    checks: List[AIOpsEvalCheck]


def list_builtin_eval_cases() -> List[Dict[str, Any]]:
    """返回内置评测场景摘要，供 API 展示。"""
    return [
        {
            "id": case.id,
            "name": case.name,
            "expected_pass": case.expected_pass,
            "plan_steps": len(case.plan),
            "evidence_steps": len(case.past_steps),
        }
        for case in build_builtin_eval_cases()
    ]


def run_builtin_eval_suite() -> Dict[str, Any]:
    """运行所有内置回放评测场景。"""
    results = [evaluate_case(case) for case in build_builtin_eval_cases()]
    passed_count = sum(1 for result in results if result.passed == result.expected_pass)
    return {
        "suite": "builtin-aiops-eval",
        "case_count": len(results),
        "matched_expectation": passed_count,
        "passed": passed_count == len(results),
        "results": [result.model_dump() for result in results],
    }


def evaluate_case(case: AIOpsEvalCase) -> AIOpsEvalResult:
    """评测单个场景，并汇总计划、证据、报告三个维度。"""
    checks = [
        _check_plan_coverage(case.plan, HIGH_CPU_REQUIRED_TOOLS),
        _check_evidence_sufficiency(case.input_text, case.past_steps),
        _check_report_safety(case.report),
    ]
    passed_checks = sum(1 for check in checks if check.passed)
    score = int((passed_checks / len(checks)) * 100)
    return AIOpsEvalResult(
        case_id=case.id,
        passed=all(check.passed for check in checks),
        expected_pass=case.expected_pass,
        score=score,
        checks=checks,
    )


def evaluate_agent_run(run: Dict[str, Any], timeline: List[Dict[str, Any]]) -> AIOpsEvalResult:
    """从真实 AgentRun 的 timeline 和最终报告中抽取诊断产物并执行质量审计。"""
    run_id = str(run.get("id") or "unknown")
    case = AIOpsEvalCase(
        id=f"agent_run:{run_id}",
        name=f"真实 AgentRun 质量审计 {run_id}",
        input_text=_extract_agent_run_input(run, timeline),
        plan=_extract_agent_run_plan(timeline),
        past_steps=_extract_agent_run_past_steps(timeline),
        report=str(run.get("final_report") or ""),
        expected_pass=True,
    )
    return evaluate_case(case)


def build_builtin_eval_cases() -> List[AIOpsEvalCase]:
    """构造内置回放用例，覆盖通过和失败场景。"""
    plan = _build_alert_playbook_steps(HIGH_CPU_INPUT)
    complete_steps = [
        ("query_metric_summary", "demo_cpu_load max=0.95 exceeded=True evidence_type=metric_summary"),
        ("get_service_health", "up{service=\"demo-service\"}=1 service_health ok"),
        ("find_error_patterns", "返回条目数: 0，未发现 error/exception/timeout/failed 日志证据"),
        ("find_fault_signals", "WARNING cpu spike injected cpu_load=high fault_signals total_signal_samples=1"),
        ("query_metric_summary", "demo_average_latency_seconds max=0.05 demo_error_total last=0 demo_fault_error_mode last=0"),
        ("propose_remediation", "requires_approval=True action=set_cpu_spike parameters={enabled:false}"),
    ]
    safe_report = "\n".join(
        [
            "# 诊断报告",
            "## 证据链",
            "- CPU 指标已超过阈值。",
            "- 未发现 error/exception/timeout/failed 日志证据。",
            "- 发现 cpu spike injected warning 线索。",
            "## 根因判断",
            "已证实 CPU 高负载；未发现错误日志，但当前证据只说明没有命中 error 级别日志。",
        ]
    )
    bad_report = safe_report.replace(
        "未发现错误日志，但当前证据只说明没有命中 error 级别日志。",
        "未发现错误日志，因此排除应用层问题。",
    )

    return [
        AIOpsEvalCase(
            id="high_cpu_complete",
            name="HighCPUUsage 完整证据链应通过",
            input_text=HIGH_CPU_INPUT,
            plan=plan,
            past_steps=complete_steps,
            report=safe_report,
            expected_pass=True,
        ),
        AIOpsEvalCase(
            id="high_cpu_missing_fault_signal",
            name="HighCPUUsage 缺少故障线索日志应失败",
            input_text=HIGH_CPU_INPUT,
            plan=plan,
            past_steps=[step for step in complete_steps if step[0] != "find_fault_signals"],
            report=safe_report,
            expected_pass=False,
        ),
        AIOpsEvalCase(
            id="high_cpu_bad_report_excludes_app",
            name="HighCPUUsage 报告错误排除应用层应失败",
            input_text=HIGH_CPU_INPUT,
            plan=plan,
            past_steps=complete_steps,
            report=bad_report,
            expected_pass=False,
        ),
    ]


def _extract_agent_run_input(run: Dict[str, Any], timeline: List[Dict[str, Any]]) -> str:
    """从 plan_created 事件中提取原始告警摘要，缺失时回退到 Run 基本信息。"""
    for event in timeline:
        payload = event.get("payload") or {}
        trace = payload.get("planner_trace") or {}
        for key in ("input_preview", "input", "raw_input"):
            if trace.get(key):
                return str(trace[key])
            if payload.get(key):
                return str(payload[key])
    return " ".join(
        str(value)
        for value in [run.get("incident_id"), run.get("model_provider"), run.get("model_name")]
        if value
    )


def _extract_agent_run_plan(timeline: List[Dict[str, Any]]) -> List[str]:
    """从真实 timeline 的 plan_created 事件中提取计划步骤。"""
    for event in timeline:
        if event.get("type") != "plan_created":
            continue
        payload = event.get("payload") or {}
        trace = payload.get("planner_trace") or {}
        plan = payload.get("plan") or trace.get("plan_steps") or trace.get("plan") or []
        if isinstance(plan, list):
            return [str(step) for step in plan if str(step).strip()]
    return []


def _extract_agent_run_past_steps(timeline: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """从真实 timeline 的 tool_executed 事件中提取工具证据。"""
    past_steps: List[Tuple[str, str]] = []
    for event in timeline:
        if event.get("type") != "tool_executed":
            continue
        payload = event.get("payload") or {}
        execution = payload.get("execution_event") or payload
        tool_calls = execution.get("tool_calls") or []
        tool_results = execution.get("tool_results") or []
        result_text = _compact_evidence_text(execution, tool_results)

        if not tool_calls:
            past_steps.append((str(event.get("message") or "tool_executed"), result_text))
            continue

        for tool_call in tool_calls:
            tool_name = str(tool_call.get("name") or "unknown_tool")
            args_text = _json_text(tool_call.get("args") or {})
            past_steps.append((tool_name, f"args={args_text}\n{result_text}"))
    return past_steps


def _compact_evidence_text(execution: Dict[str, Any], tool_results: List[Dict[str, Any]]) -> str:
    """把工具执行结果压成 Evidence Gate 可识别的短文本。"""
    parts = [str(execution.get("step") or ""), str(execution.get("result_preview") or "")]
    for tool_result in tool_results:
        if isinstance(tool_result, dict):
            parts.append(str(tool_result.get("content_preview") or tool_result.get("content") or ""))
        else:
            parts.append(str(tool_result))
    return "\n".join(part for part in parts if part)


def _json_text(value: Any) -> str:
    """安全序列化工具参数，避免复杂对象影响评测。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _check_plan_coverage(plan: List[str], required_tools: List[str]) -> AIOpsEvalCheck:
    """检查计划是否覆盖必查工具。"""
    plan_text = "\n".join(plan)
    missing_tools = [tool for tool in required_tools if tool not in plan_text]
    return AIOpsEvalCheck(
        name="plan_coverage",
        passed=not missing_tools,
        reason="计划覆盖全部必查工具。" if not missing_tools else f"计划缺少必查工具: {', '.join(missing_tools)}",
        severity="critical" if missing_tools else "info",
        details={"missing_tools": missing_tools, "required_tools": required_tools},
    )


def _check_evidence_sufficiency(input_text: str, past_steps: List[Tuple[str, str]]) -> AIOpsEvalCheck:
    """复用 Evidence Gate 检查证据是否足够。"""
    gate_result = check_evidence_sufficiency({"input": input_text, "past_steps": past_steps})
    return AIOpsEvalCheck(
        name="evidence_sufficiency",
        passed=gate_result.passed,
        reason=gate_result.reason,
        severity="critical" if not gate_result.passed else "info",
        details={
            "missing_evidence_types": gate_result.missing_evidence_types,
            "evidence_count": gate_result.evidence_count,
        },
    )


def _check_report_safety(report: str) -> AIOpsEvalCheck:
    """检查报告是否出现危险结论或模糊排除。"""
    matched_phrases = [phrase for phrase in FORBIDDEN_REPORT_PHRASES if phrase in report]
    return AIOpsEvalCheck(
        name="report_safety",
        passed=not matched_phrases,
        reason="报告未出现危险排除结论。" if not matched_phrases else f"报告包含危险结论: {', '.join(matched_phrases)}",
        severity="critical" if matched_phrases else "info",
        details={"matched_phrases": matched_phrases},
    )
