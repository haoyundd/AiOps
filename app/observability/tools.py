"""Policy-enforced registry for read-only diagnosis tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import config
from app.domain import RiskLevel
from app.observability.client import ObservabilityClient, observability_client
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
        }
        return result, record


ops_tool_registry = ToolRegistry()
