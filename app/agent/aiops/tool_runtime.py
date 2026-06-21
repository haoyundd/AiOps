"""Agent 工具运行时。

这一层负责把 Planner 生成的显式工具步骤转换为可审计的工具调用，并在真正
执行前做风险策略校验。它的目标不是替代 MCP，而是给 Agent 加一道稳定护栏：
读证据可以自动执行，真实修复必须经过人工确认入口。
"""

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


PLACEHOLDER_VALUES = {
    "指定服务",
    "某服务",
    "目标服务",
    "service",
    "service_name",
    "指定指标",
    "某指标",
    "metric",
    "metric_name",
    "unknown",
    "未知",
}

READ_ONLY_TOOLS = {
    "get_current_time",
    "retrieve_knowledge",
    "query_cpu_metrics",
    "query_memory_metrics",
    "query_metric_range",
    "query_metric_summary",
    "query_service_logs",
    "find_error_patterns",
    "find_fault_signals",
    "get_service_health",
}

ADVISORY_TOOLS = {"propose_remediation"}
REMEDIATION_EXECUTE_TOOLS = {"execute_approved_remediation"}


@dataclass
class PlannedToolCall:
    """从计划步骤中解析出的工具调用。"""

    name: str
    args: Dict[str, Any]
    source_step: str


@dataclass
class ToolPolicyDecision:
    """工具策略判断结果。"""

    allowed: bool
    risk_level: str
    reason: str
    requires_approval: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转换为前端和测试易消费的字典。"""
        return {
            "allowed": self.allowed,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
        }


@dataclass
class ToolRuntimeResult:
    """工具运行时执行结果。"""

    handled: bool
    status: str
    result_text: str
    tool_calls: List[Dict[str, Any]]
    tool_results: List[Dict[str, Any]]
    policy_decision: Optional[ToolPolicyDecision] = None


def parse_planned_tool_call(step: str) -> Optional[PlannedToolCall]:
    """从中文计划步骤中解析 `tool_name(arg=value)` 形式的显式工具调用。"""
    match = re.search(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)", step, flags=re.DOTALL)
    if not match:
        return None

    tool_name = match.group(1)
    args_text = match.group(2).strip()
    args = _parse_call_args(args_text)
    return PlannedToolCall(name=tool_name, args=args, source_step=step)


def evaluate_tool_policy(
    tool_name: str,
    args: Dict[str, Any] | None = None,
    execution_mode: str = "agent",
) -> ToolPolicyDecision:
    """根据工具名、参数和执行模式判断是否允许执行。"""
    args = args or {}

    placeholder_path = _find_placeholder_arg(args)
    if placeholder_path:
        return ToolPolicyDecision(
            allowed=False,
            risk_level="invalid",
            reason=f"参数 {placeholder_path} 仍是占位词，拒绝执行工具，避免查错服务或指标。",
        )

    if tool_name in READ_ONLY_TOOLS:
        return ToolPolicyDecision(
            allowed=True,
            risk_level="low",
            reason="观测或知识检索类工具，只读取证据，允许 Agent 自动执行。",
        )

    if tool_name in ADVISORY_TOOLS:
        return ToolPolicyDecision(
            allowed=True,
            risk_level="medium",
            reason="修复建议工具只生成待确认动作，不直接修改系统状态。",
            requires_approval=True,
        )

    if tool_name in REMEDIATION_EXECUTE_TOOLS:
        approved = bool(args.get("approved"))
        if execution_mode == "manual_remediation" and approved:
            return ToolPolicyDecision(
                allowed=True,
                risk_level="high",
                reason="真实修复动作来自人工确认入口，允许执行白名单动作。",
                requires_approval=True,
            )
        return ToolPolicyDecision(
            allowed=False,
            risk_level="high",
            reason="真实修复动作禁止由 Agent 自动执行，必须走人工确认入口。",
            requires_approval=True,
        )

    return ToolPolicyDecision(
        allowed=True,
        risk_level="unknown",
        reason="未登记风险等级的工具，按兼容模式允许执行，但会记录为 unknown。",
    )


async def execute_planned_tool_step(step: str, tools: Iterable[Any]) -> Optional[ToolRuntimeResult]:
    """如果计划步骤包含显式工具调用，则由运行时直接校验并执行。"""
    planned_call = parse_planned_tool_call(step)
    if planned_call is None:
        return None

    policy = evaluate_tool_policy(planned_call.name, planned_call.args, execution_mode="agent")
    tool_call_summary = {
        "name": planned_call.name,
        "args": planned_call.args,
        "id": None,
        "runtime": "deterministic",
    }

    if not policy.allowed:
        result_text = f"工具调用被策略阻断：{policy.reason}"
        return ToolRuntimeResult(
            handled=True,
            status="blocked",
            result_text=result_text,
            tool_calls=[tool_call_summary],
            tool_results=[],
            policy_decision=policy,
        )

    tool = find_tool_by_name(tools, planned_call.name)
    if tool is None:
        result_text = f"工具 {planned_call.name} 不可用，无法执行当前步骤。"
        return ToolRuntimeResult(
            handled=True,
            status="failed",
            result_text=result_text,
            tool_calls=[tool_call_summary],
            tool_results=[],
            policy_decision=policy,
        )

    result = await tool.ainvoke(planned_call.args)
    result_text = _stringify_result(result)
    return ToolRuntimeResult(
        handled=True,
        status="success",
        result_text=result_text,
        tool_calls=[tool_call_summary],
        tool_results=[
            {
                "name": planned_call.name,
                "tool_call_id": None,
                "content_preview": result_text[:1000],
                "content_chars": len(result_text),
                "runtime": "deterministic",
            }
        ],
        policy_decision=policy,
    )


def precheck_tool_calls(tool_calls: List[Any]) -> Optional[ToolRuntimeResult]:
    """在 ToolNode 执行模型工具调用前做统一策略预检。"""
    for call in tool_calls:
        name, args, call_id = _extract_tool_call(call)
        policy = evaluate_tool_policy(name, args, execution_mode="agent")
        if policy.allowed:
            continue

        result_text = f"工具调用被策略阻断：{policy.reason}"
        return ToolRuntimeResult(
            handled=True,
            status="blocked",
            result_text=result_text,
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": call_id,
                    "runtime": "llm_precheck",
                }
            ],
            tool_results=[],
            policy_decision=policy,
        )
    return None


def find_tool_by_name(tools: Iterable[Any], tool_name: str) -> Any | None:
    """从 LangChain 工具列表中按名称查找工具。"""
    for tool in tools:
        if getattr(tool, "name", "") == tool_name:
            return tool
    return None


def _parse_call_args(args_text: str) -> Dict[str, Any]:
    """解析函数调用参数，优先使用 Python AST，避免手写字符串切分。"""
    if not args_text:
        return {}

    try:
        expression = ast.parse(f"tool({args_text})", mode="eval")
    except SyntaxError:
        return {}

    call = expression.body
    if not isinstance(call, ast.Call):
        return {}

    args: Dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        try:
            args[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            args[keyword.arg] = ast.unparse(keyword.value)
    return args


def _find_placeholder_arg(args: Dict[str, Any], prefix: str = "") -> str:
    """递归检查参数里是否还有占位词。"""
    for key, value in args.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            nested_path = _find_placeholder_arg(value, path)
            if nested_path:
                return nested_path
        elif isinstance(value, str) and value.strip().lower() in PLACEHOLDER_VALUES:
            return path
    return ""


def _extract_tool_call(call: Any) -> tuple[str, Dict[str, Any], Any]:
    """兼容 dict 和 LangChain tool call 对象，提取名称、参数和 id。"""
    if isinstance(call, dict):
        name = call.get("name") or call.get("function", {}).get("name") or "unknown_tool"
        args = call.get("args") or call.get("arguments") or {}
        call_id = call.get("id")
    else:
        name = getattr(call, "name", "unknown_tool")
        args = getattr(call, "args", {}) or {}
        call_id = getattr(call, "id", None)

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return str(name), dict(args), call_id


def _stringify_result(result: Any) -> str:
    """把工具返回值稳定转成字符串，优先保留中文和结构化字段。"""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        return str(result)
