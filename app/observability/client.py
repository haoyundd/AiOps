"""Bounded, read-only clients for Prometheus, Loki, Tempo, and health endpoints."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import config
from app.schemas import ToolResult


def now_utc() -> datetime:
    return datetime.now(UTC)


def _tool_result(
    source: str,
    status: str,
    *,
    query: str = "",
    time_range: dict[str, str] | None = None,
    data: Any = None,
    summary: str = "",
    error: str = "",
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        tool_call_id=str(uuid.uuid4()),
        source=source,
        status=status,
        query=query,
        time_range=time_range or {},
        observed_at=now_utc(),
        data={} if data is None else data,
        summary=summary,
        error=error,
        truncated=truncated,
    )


def _latest_scalar(payload: dict[str, Any]) -> float | None:
    try:
        results = payload["data"]["result"]
        if not results:
            return None
        value = results[0].get("value")
        if value:
            return float(value[1])
        values = results[0].get("values") or []
        return float(values[-1][1]) if values else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class ObservabilityClient:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout or config.observability_timeout_seconds

    async def _get(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError("observability source returned a non-object response")

    @staticmethod
    def bounded_window(
        start: datetime | None = None, end: datetime | None = None
    ) -> tuple[datetime, datetime]:
        end = end or now_utc()
        start = start or end - timedelta(minutes=15)
        if end < start:
            raise ValueError("end time must be after start time")
        maximum = timedelta(minutes=config.max_query_range_minutes)
        if end - start > maximum:
            start = end - maximum
        return start, end

    async def query_prometheus(
        self, query: str, *, source_name: str = "prometheus"
    ) -> ToolResult:
        if not query.strip():
            return _tool_result(source_name, "rejected", error="query is empty")
        try:
            payload = await self._get(
                f"{config.prometheus_url.rstrip('/')}/api/v1/query", params={"query": query}
            )
            return _tool_result(
                source_name,
                "success",
                query=query,
                data=payload.get("data", {}),
                summary="Prometheus query completed",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                source_name,
                "unavailable",
                query=query,
                summary="Prometheus source is unavailable",
                error=str(exc),
            )

    async def query_prometheus_range(
        self,
        query: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        step: str = "30s",
    ) -> ToolResult:
        start, end = self.bounded_window(start, end)
        window = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            payload = await self._get(
                f"{config.prometheus_url.rstrip('/')}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": step,
                },
            )
            return _tool_result(
                "prometheus",
                "success",
                query=query,
                time_range=window,
                data=payload.get("data", {}),
                summary="Prometheus range query completed",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                "prometheus",
                "unavailable",
                query=query,
                time_range=window,
                summary="Prometheus range query is unavailable",
                error=str(exc),
            )

    async def get_red_metrics(self, service_name: str) -> ToolResult:
        selector = f'job="{service_name}"'
        queries = {
            "request_rate": (
                f'sum(rate(http_server_requests_seconds_count{{{selector}}}[5m]))'
            ),
            "error_rate": (
                f'sum(rate(http_server_requests_seconds_count{{{selector},status=~"5.."}}[5m])) '
                f'/ clamp_min(sum(rate(http_server_requests_seconds_count{{{selector}}}[5m])), 0.001)'
            ),
            "latency_p95_seconds": (
                "histogram_quantile(0.95, sum by (le) "
                f'(rate(http_server_requests_seconds_bucket{{{selector}}}[5m])))'
            ),
        }
        results = await asyncio.gather(*(self.query_prometheus(query) for query in queries.values()))
        if all(item.status != "success" for item in results):
            return _tool_result(
                "prometheus",
                "unavailable",
                query="; ".join(queries.values()),
                error="all RED metric queries failed",
                data={
                    name: result.model_dump(mode="json")
                    for name, result in zip(queries, results, strict=True)
                },
            )
        values: dict[str, float | None] = {}
        for name, result in zip(queries, results, strict=True):
            values[name] = _latest_scalar({"data": result.data}) if result.status == "success" else None
        return _tool_result(
            "prometheus",
            "success",
            query="; ".join(queries.values()),
            data=values,
            summary=(
                f"request_rate={values['request_rate']}, error_rate={values['error_rate']}, "
                f"p95={values['latency_p95_seconds']}s"
            ),
        )

    async def get_jvm_metrics(self, service_name: str) -> ToolResult:
        selector = f'job="{service_name}"'
        queries = {
            "process_cpu_usage": f'max(process_cpu_usage{{{selector}}})',
            "system_cpu_usage": f'max(system_cpu_usage{{{selector}}})',
            "heap_used_bytes": f'sum(jvm_memory_used_bytes{{{selector},area="heap"}})',
            "live_threads": f'max(jvm_threads_live_threads{{{selector}}})',
        }
        results = await asyncio.gather(*(self.query_prometheus(query) for query in queries.values()))
        values: dict[str, float | None] = {}
        for name, result in zip(queries, results, strict=True):
            values[name] = _latest_scalar({"data": result.data}) if result.status == "success" else None
        if all(value is None for value in values.values()):
            return _tool_result(
                "prometheus",
                "unavailable",
                query="; ".join(queries.values()),
                error="JVM/process metrics are unavailable",
                data=values,
            )
        return _tool_result(
            "prometheus",
            "success",
            query="; ".join(queries.values()),
            data=values,
            summary=(
                f"process_cpu={values['process_cpu_usage']}, heap={values['heap_used_bytes']}, "
                f"threads={values['live_threads']}"
            ),
        )

    async def query_loki_logs(
        self,
        service_name: str,
        *,
        keyword: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> ToolResult:
        start, end = self.bounded_window(start, end)
        limit = max(1, min(limit, config.max_query_results))
        escaped = keyword.replace('"', '\\"')
        query = f'{{service_name="{service_name}"}}'
        if escaped:
            query += f' |~ "(?i){escaped}"'
        window = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            payload = await self._get(
                f"{config.loki_url.rstrip('/')}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": int(start.timestamp() * 1_000_000_000),
                    "end": int(end.timestamp() * 1_000_000_000),
                    "limit": limit,
                    "direction": "backward",
                },
            )
            streams = payload.get("data", {}).get("result", [])
            samples: list[dict[str, Any]] = []
            for stream in streams:
                labels = stream.get("stream", {})
                for timestamp, line in stream.get("values", []):
                    samples.append({"timestamp": timestamp, "line": line, "labels": labels})
                    if len(samples) >= limit:
                        break
            return _tool_result(
                "loki",
                "success",
                query=query,
                time_range=window,
                data={"samples": samples},
                summary=f"Loki returned {len(samples)} log samples",
                truncated=len(samples) >= limit,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                "loki",
                "unavailable",
                query=query,
                time_range=window,
                summary="Loki log source is unavailable",
                error=str(exc),
            )

    async def get_log_error_patterns(self, service_name: str) -> ToolResult:
        result = await self.query_loki_logs(
            service_name,
            keyword="error|exception|timeout|refused|unavailable|redis|mysql|rocketmq",
            limit=100,
        )
        if result.status != "success":
            return result
        patterns = {
            "redis": 0,
            "mysql": 0,
            "rocketmq": 0,
            "timeout": 0,
            "connection_refused": 0,
            "out_of_memory": 0,
        }
        samples = result.data.get("samples", []) if isinstance(result.data, dict) else []
        for sample in samples:
            line = str(sample.get("line", "")).lower()
            patterns["redis"] += int("redis" in line or "lettuce" in line)
            patterns["mysql"] += int("mysql" in line or "jdbc" in line)
            patterns["rocketmq"] += int("rocketmq" in line or "namesrv" in line)
            patterns["timeout"] += int("timeout" in line or "timed out" in line)
            patterns["connection_refused"] += int(
                "connection refused" in line or "cannot connect" in line
            )
            patterns["out_of_memory"] += int("outofmemory" in line)
        result.data = {"patterns": patterns, "samples": samples[:20]}
        result.summary = ", ".join(f"{name}={count}" for name, count in patterns.items())
        return result

    async def search_tempo_traces(
        self,
        service_name: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
    ) -> ToolResult:
        start, end = self.bounded_window(start, end)
        limit = max(1, min(limit, 100))
        query = f'service.name="{service_name}"'
        window = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            payload = await self._get(
                f"{config.tempo_url.rstrip('/')}/api/search",
                params={
                    "tags": query,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "limit": limit,
                },
            )
            traces = payload.get("traces", [])
            return _tool_result(
                "tempo",
                "success",
                query=query,
                time_range=window,
                data={"traces": traces},
                summary=f"Tempo returned {len(traces)} trace summaries",
                truncated=len(traces) >= limit,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                "tempo",
                "unavailable",
                query=query,
                time_range=window,
                summary="Tempo trace source is unavailable",
                error=str(exc),
            )

    async def get_trace_detail(self, trace_id: str) -> ToolResult:
        if not re.fullmatch(r"[0-9a-fA-F]{16,32}", trace_id):
            return _tool_result("tempo", "rejected", error="invalid trace id")
        try:
            payload = await self._get(f"{config.tempo_url.rstrip('/')}/api/traces/{trace_id}")
            return _tool_result(
                "tempo",
                "success",
                query=trace_id,
                data=payload,
                summary=f"Trace {trace_id} loaded",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                "tempo",
                "unavailable",
                query=trace_id,
                summary="Tempo trace detail is unavailable",
                error=str(exc),
            )

    async def get_service_health(self, health_url: str) -> ToolResult:
        started = time.perf_counter()
        try:
            payload = await self._get(health_url)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            state = str(payload.get("status", "UNKNOWN")).upper()
            return _tool_result(
                "health",
                "success",
                query=health_url,
                data={"state": state, "latency_ms": elapsed_ms, "details": payload},
                summary=f"service health={state}, latency={elapsed_ms}ms",
            )
        except (httpx.HTTPError, ValueError) as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return _tool_result(
                "health",
                "unavailable",
                query=health_url,
                data={"state": "DOWN", "latency_ms": elapsed_ms},
                error=str(exc),
                summary=f"health endpoint unavailable after {elapsed_ms}ms",
            )


observability_client = ObservabilityClient()
