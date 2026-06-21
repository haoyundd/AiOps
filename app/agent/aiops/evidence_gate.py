"""AIOps Evidence Gate。

Evidence Gate 是 Harness 层的一道闸：它不判断根因，只判断当前证据是否足以生成
确定性报告，避免模型把“没查到错误日志”误写成“排除应用层问题”。
"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class EvidenceGateResult(BaseModel):
    """证据充分性检查结果。"""

    passed: bool = Field(description="当前证据是否足以生成确定性报告")
    reason: str = Field(description="判断原因，供 Replanner 和前端展示")
    missing_evidence_types: List[str] = Field(default_factory=list, description="缺失证据类型")
    evidence_count: int = Field(default=0, description="已命中的核心证据数量")


def check_evidence_sufficiency(state: Dict[str, Any]) -> EvidenceGateResult:
    """检查当前 AIOps 状态是否满足最低证据门槛。"""
    input_text = str(state.get("input", ""))
    past_steps = state.get("past_steps", [])
    executed_text = "\n".join(f"{step}\n{result}" for step, result in past_steps).lower()

    if "highcpuusage" in input_text.lower() or "demo_cpu_load" in input_text:
        return _check_high_cpu_evidence(executed_text)

    evidence_keywords = {
        "metric": ("query_metric_summary", "query_metric_range", "query_cpu_metrics"),
        "log": ("query_service_logs", "find_error_patterns", "find_fault_signals"),
        "health": ("get_service_health",),
    }
    matched = [
        evidence_type
        for evidence_type, keywords in evidence_keywords.items()
        if any(keyword in executed_text for keyword in keywords)
    ]
    return EvidenceGateResult(
        passed=len(matched) >= 2,
        reason="已收集至少两类核心证据。" if len(matched) >= 2 else "核心证据不足，不能生成确定性报告。",
        missing_evidence_types=[
            evidence_type for evidence_type in evidence_keywords if evidence_type not in matched
        ],
        evidence_count=len(matched),
    )


def _check_high_cpu_evidence(executed_text: str) -> EvidenceGateResult:
    """检查 HighCPUUsage 是否已经完成 CPU 场景的核心证据链。"""
    requirements = {
        "metric_summary": ("query_metric_summary", "query_cpu_metrics", "demo_cpu_load"),
        "service_health": ("get_service_health", 'up{service=', "service_health"),
        "error_log_check": ("find_error_patterns", "error_patterns"),
        "fault_signal_log": ("find_fault_signals", "fault_signals", "cpu spike", "cpu_load=high"),
    }
    missing = [
        evidence_type
        for evidence_type, keywords in requirements.items()
        if not any(keyword in executed_text for keyword in keywords)
    ]
    passed = not missing
    return EvidenceGateResult(
        passed=passed,
        reason=(
            "HighCPUUsage 核心证据已齐：指标、健康、错误日志检查和故障线索日志均已覆盖。"
            if passed
            else "HighCPUUsage 证据不足，必须补齐指标、健康、错误日志检查和故障线索日志后再下确定性结论。"
        ),
        missing_evidence_types=missing,
        evidence_count=len(requirements) - len(missing),
    )
