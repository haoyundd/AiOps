"""Policy-enforced registry for read-only diagnosis tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import config
from app.domain import RiskLevel
from app.observability.client import ObservabilityClient, observability_client
from app.observability.mcp_client import MCPProtocolError, StreamableHTTPMCPClient
from app.schemas import ToolPolicy, ToolResult

ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    source: str
    policy: ToolPolicy
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, client: ObservabilityClient | None = None) -> None:
        """注册只读工具，并为每个工具附加超时、重试和风险策略。"""
        self.client = client or observability_client
        readonly = ToolPolicy(
            risk_level=RiskLevel.READ_ONLY,
            read_only=True,
            timeout_seconds=config.observability_timeout_seconds,
            retries=1,
            max_time_range_minutes=config.max_query_range_minutes,
            max_result_count=config.max_query_results,
        )
        self._tools: dict[str, ToolDefinition] = {
            "get_service_health": ToolDefinition(
                "get_service_health", "health", readonly, self.client.get_service_health
            ),
            "get_red_metrics": ToolDefinition(
                "get_red_metrics", "prometheus", readonly, self.client.get_red_metrics
            ),
            "get_jvm_metrics": ToolDefinition(
                "get_jvm_metrics", "prometheus", readonly, self.client.get_jvm_metrics
            ),
            "get_messaging_metrics": ToolDefinition(
                "get_messaging_metrics",
                "prometheus",
                readonly,
                self.client.get_messaging_metrics,
            ),
            "get_top_endpoint_metrics": ToolDefinition(
                "get_top_endpoint_metrics",
                "prometheus",
                readonly,
                self.client.get_top_endpoint_metrics,
            ),
            "get_jvm_thread_snapshot": ToolDefinition(
                "get_jvm_thread_snapshot",
                "jvm",
                readonly,
                self.client.get_jvm_thread_snapshot,
            ),
            "query_prometheus_range": ToolDefinition(
                "query_prometheus_range",
                "prometheus",
                readonly,
                self.client.query_prometheus_range,
            ),
            "query_loki_logs": ToolDefinition(
                "query_loki_logs", "loki", readonly, self.client.query_loki_logs
            ),
            "get_log_error_patterns": ToolDefinition(
                "get_log_error_patterns",
                "loki",
                readonly,
                self.client.get_log_error_patterns,
            ),
            "search_tempo_traces": ToolDefinition(
                "search_tempo_traces",
                "tempo",
                readonly,
                self.client.search_tempo_traces,
            ),
            "get_trace_detail": ToolDefinition(
                "get_trace_detail", "tempo", readonly, self.client.get_trace_detail
            ),
        }

    def definitions(self) -> list[ToolDefinition]:
        """返回工具定义，供测试和后续工具发现使用。"""
        return list(self._tools.values())

    async def execute(self, name: str, **kwargs: Any) -> tuple[ToolResult, dict[str, Any]]:
        definition = self._tools.get(name)
        if definition is None:
            raise KeyError(f"unknown tool: {name}")
        if not definition.policy.read_only or definition.policy.risk_level != RiskLevel.READ_ONLY:
            raise PermissionError(f"mutating tool cannot be used by diagnosis agent: {name}")

        started = time.perf_counter()
        attempts = definition.policy.retries + 1
        result: ToolResult | None = None
        for attempt in range(attempts):
            try:
                result = await asyncio.wait_for(
                    definition.handler(**kwargs), timeout=definition.policy.timeout_seconds
                )
            except TimeoutError:
                result = ToolResult(
                    tool_call_id=f"timeout-{name}-{attempt}",
                    source=definition.source,
                    status="unavailable",
                    observed_at=datetime.now(UTC),
                    summary=f"{definition.source} tool timed out",
                    error=f"tool timed out after {definition.policy.timeout_seconds}s",
                )
            if result.status == "success" or attempt == attempts - 1:
                break
        assert result is not None
        duration_ms = int((time.perf_counter() - started) * 1000)
        record = {
            "tool_name": name,
            "source": definition.source,
            "risk_level": definition.policy.risk_level,
            "status": result.status,
            "input": kwargs,
            "output": result.model_dump(mode="json"),
            "error": result.error,
            "duration_ms": duration_ms,
            "transport": "local",
        }
        return result, record


ops_tool_registry = ToolRegistry()


class MCPToolRegistry:
    """将 MCP 工具包装成与本地 Registry 相同的审计记录格式。"""

    _sources = {
        "get_service_health": "health",
        "get_red_metrics": "prometheus",
        "get_jvm_metrics": "prometheus",
        "get_messaging_metrics": "prometheus",
        "get_top_endpoint_metrics": "prometheus",
        "get_jvm_thread_snapshot": "jvm",
        "query_prometheus_range": "prometheus",
        "query_loki_logs": "loki",
        "get_log_error_patterns": "loki",
        "search_tempo_traces": "tempo",
        "get_trace_detail": "tempo",
    }

    def __init__(self) -> None:
        """创建指向 mcp-ops 的客户端，生产不会直接连接观测后端。"""
        self.client = StreamableHTTPMCPClient(
            config.mcp_ops_url, config.observability_timeout_seconds
        )

    async def execute(self, name: str, **kwargs: Any) -> tuple[ToolResult, dict[str, Any]]:
        """执行工具并返回结果与审计记录；高风险工具会在调用前被拒绝。"""
        """通过 MCP 调用只读工具；不可用时只返回 unavailable，不绕过 MCP。"""
        if name not in self._sources:
            raise KeyError(f"unknown MCP tool: {name}")
        started = time.perf_counter()
        try:
            payload = await asyncio.wait_for(
                self.client.call_tool(name, kwargs), timeout=config.observability_timeout_seconds
            )
            result = ToolResult.model_validate(payload)
        except (TimeoutError, httpx.HTTPError, ValueError, MCPProtocolError) as exc:
            result = ToolResult(
                tool_call_id=f"mcp-unavailable-{name}",
                source=self._sources[name],
                status="unavailable",
                observed_at=datetime.now(UTC),
                summary=f"MCP tool {name} is unavailable",
                error=str(exc),
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        record = {
            "tool_name": name,
            "source": self._sources[name],
            "risk_level": RiskLevel.READ_ONLY,
            "status": result.status,
            "input": kwargs,
            "output": result.model_dump(mode="json"),
            "error": result.error,
            "duration_ms": duration_ms,
            "transport": "mcp",
        }
        return result, record


mcp_ops_registry = MCPToolRegistry()
