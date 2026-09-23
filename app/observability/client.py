"""Bounded, read-only clients for Prometheus, Loki, Tempo, and health endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import config
from app.schemas import ToolResult


def now_utc() -> datetime:
    """返回带时区的 UTC 时间，保证所有观测结果可按时间窗口查询。"""
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
    """创建统一的观测工具返回对象，避免不同数据源暴露不同协议。"""
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
    """从 Prometheus instant/range response 中取最新标量值。"""
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


def _number(value: Any) -> float | None:
    """将 Tempo 的字符串数值安全转换为浮点数。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    """把 MCP 传入的 ISO 字符串转换成带时区时间，避免跨进程传 datetime 失败。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _attribute_value(item: dict[str, Any]) -> Any:
    """读取 OTLP 属性的 string/int/bool/double 值。"""
    value = item.get("value") or {}
    for key in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    return None


def _sanitize_route(value: Any) -> str:
    """保留框架路由模板，并把误入标签的数字/UUID 段折叠为 {id}。"""
    route = str(value or "").strip().split("?", 1)[0]
    if not route.startswith("/") or "://" in route:
        return ""
    route = re.sub(
        r"(?<=/)(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})(?=/|$)",
        "{id}",
        route,
    )
    return route[:200]


def _safe_http_method(value: Any) -> str:
    """只接受标准的大写 HTTP 方法，拒绝把任意属性文本写入证据。"""
    method = str(value or "").upper()
    return method if re.fullmatch(r"[A-Z]{3,10}", method) else ""


def _integer(value: Any) -> int | None:
    """把 OTLP 状态码安全转换为整数。"""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_trace_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """将 Tempo OTLP batches 展平为 Agent 可排序的下游 Span 摘要。"""
    spans: list[dict[str, Any]] = []
    for batch in payload.get("batches", []):
        resource_attributes = {
            item.get("key"): _attribute_value(item)
            for item in (batch.get("resource", {}).get("attributes", []) or [])
            if item.get("key")
        }
        service_name = resource_attributes.get("service.name", "")
        for scope_batch in batch.get("scopeSpans", []) or []:
            scope_name = (scope_batch.get("scope") or {}).get("name", "")
            for span in scope_batch.get("spans", []) or []:
                attributes = {
                    item.get("key"): _attribute_value(item)
                    for item in (span.get("attributes", []) or [])
                    if item.get("key")
                }
                start = _number(span.get("startTimeUnixNano"))
                end = _number(span.get("endTimeUnixNano"))
                duration_ms = (end - start) / 1_000_000 if start is not None and end else 0.0
                status = span.get("status") or {}
                method = _safe_http_method(
                    attributes.get("http.request.method") or attributes.get("http.method")
                )
                route = _sanitize_route(attributes.get("http.route"))
                if not route:
                    # 某些 Java Agent 只把模板写在 Server Span 名称中，不读取 url.path，
                    # 避免把真实用户 ID、查询参数或 Token 持久化到诊断库。
                    name_match = re.fullmatch(r"([A-Z]{3,10})\s+(/[^?]*)", str(span.get("name", "")))
                    if name_match:
                        method = method or _safe_http_method(name_match.group(1))
                        route = _sanitize_route(name_match.group(2))
                status_code = _integer(
                    attributes.get("http.response.status_code")
                    or attributes.get("http.status_code")
                )
                error_type = str(attributes.get("error.type") or "").replace("\n", " ")[:160]
                safe_attribute_names = {
                    "db.system",
                    "db.operation.name",
                    "messaging.system",
                    "messaging.operation.type",
                    "rpc.system",
                    "server.address",
                    "net.sock.peer.name",
                    "http.route",
                    "http.request.method",
                    "http.method",
                    "http.response.status_code",
                    "http.status_code",
                    "error.type",
                }
                safe_attributes = {
                    key: value for key, value in attributes.items() if key in safe_attribute_names
                }
                spans.append(
                    {
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "parent_span_id": span.get("parentSpanId", ""),
                        "name": span.get("name", ""),
                        "kind": span.get("kind", ""),
                        "scope": scope_name,
                        "service_name": service_name,
                        "duration_ms": round(duration_ms, 3),
                        "status": status.get("code", "UNSET"),
                        "http_route": route,
                        "http_method": method,
                        "http_status_code": status_code,
                        "error_type": error_type,
                        "attributes": safe_attributes,
                    }
                )
    return spans


class ObservabilityClient:
    def __init__(self, timeout: float | None = None) -> None:
        """初始化带统一超时的只读观测客户端。"""
        self.timeout = timeout or config.observability_timeout_seconds

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """执行 GET 并拒绝非 JSON 对象响应，避免把异常页面当成证据。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError("observability source returned a non-object response")

    @staticmethod
    def bounded_window(
        start: datetime | None = None, end: datetime | None = None
    ) -> tuple[datetime, datetime]:
        """限制查询时间窗口，防止一次诊断读取过多历史数据。"""
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

    async def get_messaging_metrics(self, service_name: str) -> ToolResult:
        """查询 RocketMQ 发布失败计数，并按固定业务流返回低基数结果。"""
        query = (
            "sum by (flow) (increase(merchantflow_mq_publish_failures_total"
            f'{{job="{service_name}"}}[5m]))'
        )
        result = await self.query_prometheus(query)
        if result.status != "success":
            return _tool_result(
                "prometheus",
                "unavailable",
                query=query,
                data={"mq_publish_failures_5m": None, "failures_by_flow": {}},
                summary="RocketMQ publish failure metrics are unavailable",
                error=result.error,
            )

        failures_by_flow: dict[str, float] = {}
        for row in (result.data or {}).get("result", []):
            flow = str((row.get("metric") or {}).get("flow") or "other")
            value = _latest_scalar({"data": {"result": [row]}})
            if value is not None:
                failures_by_flow[flow] = value
        total = sum(failures_by_flow.values())
        return _tool_result(
            "prometheus",
            "success",
            query=query,
            data={
                "mq_publish_failures_5m": total,
                "failures_by_flow": failures_by_flow,
            },
            summary=f"RocketMQ publish failures in 5m={total}",
        )

    async def get_top_endpoint_metrics(
        self, service_name: str, limit: int = 10
    ) -> ToolResult:
        """按 method/uri 合并请求量、P95 和 5xx 比例，最多返回十个路由模板。"""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name):
            return _tool_result("prometheus", "rejected", error="invalid service name")
        safe_limit = min(max(limit, 1), 10)
        selector = f'job="{service_name}",uri!~"/actuator.*"'
        queries = {
            "request_rate": (
                f"topk({safe_limit}, sum by (method, uri) "
                f"(rate(http_server_requests_seconds_count{{{selector}}}[5m])))"
            ),
            "latency_p95_seconds": (
                f"topk({safe_limit}, histogram_quantile(0.95, sum by (le, method, uri) "
                f"(rate(http_server_requests_seconds_bucket{{{selector}}}[5m]))))"
            ),
            "error_rate": (
                f"topk({safe_limit}, sum by (method, uri) "
                f"(rate(http_server_requests_seconds_count{{{selector},status=~\"5..\"}}[5m])) "
                f"/ clamp_min(sum by (method, uri) "
                f"(rate(http_server_requests_seconds_count{{{selector}}}[5m])), 0.001))"
            ),
        }
        results = await asyncio.gather(*(self.query_prometheus(query) for query in queries.values()))
        if all(item.status != "success" for item in results):
            return _tool_result(
                "prometheus",
                "unavailable",
                query="; ".join(queries.values()),
                error="all endpoint metric queries failed",
            )

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for metric_name, result in zip(queries, results, strict=True):
            if result.status != "success":
                continue
            for row in (result.data or {}).get("result", []):
                labels = row.get("metric") or {}
                method = _safe_http_method(labels.get("method"))
                route = _sanitize_route(labels.get("uri"))
                value = _latest_scalar({"data": {"result": [row]}})
                if not method or not route or value is None:
                    continue
                endpoint = merged.setdefault(
                    (method, route),
                    {
                        "method": method,
                        "uri": route,
                        "request_rate": None,
                        "latency_p95_seconds": None,
                        "error_rate": None,
                    },
                )
                endpoint[metric_name] = value

        endpoints = sorted(
            merged.values(),
            key=lambda item: float(item.get("request_rate") or 0),
            reverse=True,
        )[:safe_limit]
        return _tool_result(
            "prometheus",
            "success",
            query="; ".join(queries.values()),
            data={"endpoints": endpoints},
            summary=f"Prometheus returned {len(endpoints)} endpoint metric rows",
            truncated=len(merged) > safe_limit,
        )

    async def get_jvm_thread_snapshot(self, service_name: str) -> ToolResult:
        """使用 AK/SK 调用 MerchantFlow 内部线程快照，并再次裁剪返回规模。"""
        if service_name != "merchantflow":
            return _tool_result("jvm", "rejected", error="unsupported service")
        if not config.aksk_access_key or not config.aksk_secret_key:
            return _tool_result(
                "jvm",
                "unavailable",
                summary="MerchantFlow diagnostics credentials are not configured",
                error="AK/SK credentials are missing",
            )
        path = "/internal/ops/thread-snapshot"
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        raw = f"{config.aksk_access_key}\n{timestamp}\n{nonce}\nGET\n{path}"
        signature = hmac.new(
            config.aksk_secret_key.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-AK": config.aksk_access_key,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        url = f"{config.merchantflow_ops_url.rstrip('/')}{path}"
        try:
            payload = await self._get(url, headers=headers)
            if not payload.get("success") or not isinstance(payload.get("data"), dict):
                raise ValueError("MerchantFlow rejected the diagnostics request")
            data = dict(payload["data"])
            threads: list[dict[str, Any]] = []
            for thread in (data.get("threads") or [])[:10]:
                safe_thread = dict(thread)
                safe_thread["stack_frames"] = (thread.get("stack_frames") or [])[:20]
                threads.append(safe_thread)
            data["threads"] = threads
            return _tool_result(
                "jvm",
                "success",
                query=path,
                data=data,
                summary=f"MerchantFlow returned {len(threads)} sampled JVM threads",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _tool_result(
                "jvm",
                "unavailable",
                query=path,
                summary="MerchantFlow JVM thread snapshot is unavailable",
                error=str(exc),
            )

    async def query_loki_logs(
        self,
        service_name: str,
        *,
        keyword: str = "",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 100,
    ) -> ToolResult:
        # MCP 是独立进程，时间范围以 ISO 字符串传输；非法值回退到安全的最近窗口。
        # 下一步：Loki 只返回告警窗口内的日志，避免旧故障污染当前根因判断。
        start, end = self.bounded_window(_coerce_datetime(start), _coerce_datetime(end))
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

    async def get_log_error_patterns(
        self,
        service_name: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> ToolResult:
        """统计指定告警窗口内的日志模式，不跨窗口读取历史错误。"""
        result = await self.query_loki_logs(
            service_name,
            keyword="error|exception|timeout|refused|unavailable|redis|mysql|rocketmq",
            start=start,
            end=end,
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
            "application_error": 0,
        }
        samples = result.data.get("samples", []) if isinstance(result.data, dict) else []
        for sample in samples:
            line = str(sample.get("line", "")).lower()
            # Redisson 的连接维护日志可能只出现 PingConnectionHandler，不直接写 Redis。
            # 下一步：保留原始 samples，便于后续按具体客户端版本继续补充模式。
            patterns["redis"] += int(
                "redis" in line
                or "lettuce" in line
                or "redisson" in line
                or "pingconnectionhandler" in line
            )
            patterns["mysql"] += int("mysql" in line or "jdbc" in line)
            patterns["rocketmq"] += int("rocketmq" in line or "namesrv" in line)
            patterns["timeout"] += int("timeout" in line or "timed out" in line)
            # Redis 代理被切断时，Redisson 常记录 closed channel，而不是标准的
            # "connection refused"；这同样表示依赖连接不可用，下一步交给 Evidence
            # Graph 区分 outage 与 latency，避免真实中断被误归类成普通慢请求。
            patterns["connection_refused"] += int(
                "connection refused" in line
                or "cannot connect" in line
                or "closed channel" in line
                or "stacklessclosedchannelexception" in line
                or "unable to send ping" in line
            )
            patterns["out_of_memory"] += int("outofmemory" in line)
            # 排除已经归因到基础依赖的日志，避免把 Redis/MySQL 错误重复算成应用错误。
            patterns["application_error"] += int(
                ("error" in line or "exception" in line)
                and not any(name in line for name in ("redis", "mysql", "rocketmq", "lettuce", "jdbc"))
            )
        result.data = {"patterns": patterns, "samples": samples[:20]}
        result.summary = ", ".join(f"{name}={count}" for name, count in patterns.items())
        return result

    async def search_tempo_traces(
        self,
        service_name: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 20,
    ) -> ToolResult:
        # 与 Loki 使用相同的 Incident 时间窗口，保证 Trace 和日志可以相互印证。
        # 下一步：Trace 详情继续展开窗口内 Top N 慢 Trace 的下游 Span。
        start, end = self.bounded_window(_coerce_datetime(start), _coerce_datetime(end))
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
                    # 先排除健康检查 PING 等短 Trace，优先把告警窗口里的慢请求交给 Evidence Graph。
                    # 下一步：Trace 详情会展开这些慢请求中的 Redis/MySQL/HTTP 子 Span。
                    "minDuration": "1s",
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
            spans = _extract_trace_spans(payload)
            # 下一步：Evidence Graph 会从这些 Span 中筛选具体依赖并参与根因排序。
            return _tool_result(
                "tempo",
                "success",
                query=trace_id,
                data={
                    "trace_id": trace_id,
                    "spans": spans,
                    "raw": payload,
                },
                summary=f"Trace {trace_id} loaded with {len(spans)} spans",
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
