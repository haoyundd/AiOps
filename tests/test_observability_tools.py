import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx

from app.observability.client import ObservabilityClient, _extract_trace_spans
from app.observability.tools import MCPToolRegistry, ToolDefinition, ToolRegistry
from app.schemas import ToolPolicy, ToolResult


async def test_query_window_is_bounded():
    client = ObservabilityClient()
    end = datetime.now(UTC)
    start, bounded_end = client.bounded_window(end=end, start=end - timedelta(days=3))
    assert bounded_end == end
    assert bounded_end - start <= timedelta(minutes=120)


async def test_registry_rejects_mutating_tool():
    registry = ToolRegistry()

    async def fake_mutation() -> ToolResult:
        raise AssertionError("mutation handler must never execute")

    registry._tools["dangerous"] = ToolDefinition(
        name="dangerous",
        source="shell",
        policy=ToolPolicy(read_only=False, risk_level="HIGH"),
        handler=fake_mutation,
    )
    try:
        await registry.execute("dangerous")
    except PermissionError as exc:
        assert "cannot be used" in str(exc)
    else:
        raise AssertionError("mutating tool was not blocked")


async def test_invalid_trace_id_is_rejected_without_network_call():
    result = await ObservabilityClient().get_trace_detail("not-a-trace-id")
    assert result.status == "rejected"
    assert result.source == "tempo"


async def test_all_observability_adapters_return_structured_evidence(monkeypatch):
    client = ObservabilityClient()
    tempo_search_params = {}

    async def fake_get(url, params=None):
        if "loki/api" in url:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service_name": "merchantflow"},
                            "values": [[
                                "1",
                                "o.r.client.handler.PingConnectionHandler: "
                                "Unable to send PING command over channel: "
                                "StacklessClosedChannelException",
                            ]],
                        }
                    ]
                }
            }
        if url.endswith("/api/v1/query_range"):
            return {"data": {"result": [{"values": [["1", "0.9"]]}]}}
        if url.endswith("/api/v1/query"):
            query = params["query"]
            if "merchantflow_mq_publish_failures_total" in query:
                return {
                    "data": {
                        "result": [
                            {
                                "metric": {"flow": "seckill-order"},
                                "value": ["1", "2"],
                            }
                        ]
                    }
                }
            value = "0.93" if "process_cpu_usage" in query else "1.4"
            return {"data": {"result": [{"value": ["1", value]}]}}
        if url.endswith("/api/search"):
            tempo_search_params.update(params or {})
            return {"traces": [{"traceID": "a" * 32, "durationMs": 1450}]}
        if "/api/traces/" in url:
            return {"batches": [{"resource": {"service.name": "merchantflow"}}]}
        return {"status": "UP", "components": {"redis": {"status": "UP"}}}

    monkeypatch.setattr(client, "_get", fake_get)
    metric_range = await client.query_prometheus_range("process_cpu_usage")
    red = await client.get_red_metrics("merchantflow")
    jvm = await client.get_jvm_metrics("merchantflow")
    messaging = await client.get_messaging_metrics("merchantflow")
    logs = await client.query_loki_logs("merchantflow", keyword="timeout", limit=5)
    patterns = await client.get_log_error_patterns("merchantflow")
    traces = await client.search_tempo_traces("merchantflow")
    trace = await client.get_trace_detail("a" * 32)
    health = await client.get_service_health("http://merchantflow/actuator/health")

    assert metric_range.status == "success"
    assert red.data["latency_p95_seconds"] == 1.4
    assert jvm.data["process_cpu_usage"] == 0.93
    assert messaging.data["mq_publish_failures_5m"] == 2.0
    assert messaging.data["failures_by_flow"] == {"seckill-order": 2.0}
    assert "PingConnectionHandler" in logs.data["samples"][0]["line"]
    assert patterns.data["patterns"]["redis"] == 1
    assert patterns.data["patterns"]["connection_refused"] == 1
    assert traces.data["traces"][0]["durationMs"] == 1450
    assert tempo_search_params["minDuration"] == "1s"
    assert trace.status == "success"
    assert health.data["state"] == "UP"


async def test_source_failures_are_reported_as_unavailable(monkeypatch):
    client = ObservabilityClient()

    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("source is down")

    monkeypatch.setattr(client, "_get", unavailable)
    assert (await client.query_prometheus("up")).status == "unavailable"
    assert (await client.query_prometheus_range("up")).status == "unavailable"
    assert (await client.query_loki_logs("merchantflow")).status == "unavailable"
    assert (await client.search_tempo_traces("merchantflow")).status == "unavailable"
    assert (await client.get_trace_detail("b" * 32)).status == "unavailable"
    health = await client.get_service_health("http://merchantflow/actuator/health")
    assert health.status == "unavailable"
    assert health.data["state"] == "DOWN"


async def test_top_endpoint_metrics_are_merged_bounded_and_sanitized(monkeypatch):
    """接口指标必须按路由合并，且不能把原始数字 ID 路径写入诊断证据。"""
    client = ObservabilityClient()

    async def fake_get(url, params=None):
        query = params["query"]
        if "histogram_quantile" in query:
            value = "1.2"
        elif 'status=~"5.."' in query:
            value = "0.1"
        else:
            value = "8.0"
        return {
            "data": {
                "result": [
                    {
                        "metric": {"method": "GET", "uri": "/shop/123456"},
                        "value": ["1", value],
                    }
                ]
            }
        }

    monkeypatch.setattr(client, "_get", fake_get)
    result = await client.get_top_endpoint_metrics("merchantflow", limit=99)

    assert result.status == "success"
    assert len(result.data["endpoints"]) == 1
    assert result.data["endpoints"][0] == {
        "method": "GET",
        "uri": "/shop/{id}",
        "request_rate": 8.0,
        "latency_p95_seconds": 1.2,
        "error_rate": 0.1,
    }
    assert "123456" not in str(result.data)


def test_trace_span_extraction_keeps_route_but_drops_sensitive_attributes():
    """Trace 只保留诊断所需属性，完整 URL、SQL 和用户字段不得持久化。"""
    payload = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "merchantflow"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "io.opentelemetry.tomcat"},
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "GET /shop/{id}",
                                "kind": "SPAN_KIND_SERVER",
                                "startTimeUnixNano": "1000000",
                                "endTimeUnixNano": "3000000",
                                "status": {"code": "STATUS_CODE_ERROR", "message": "timeout"},
                                "attributes": [
                                    {"key": "http.route", "value": {"stringValue": "/shop/{id}"}},
                                    {"key": "http.request.method", "value": {"stringValue": "GET"}},
                                    {"key": "http.response.status_code", "value": {"intValue": "500"}},
                                    {"key": "error.type", "value": {"stringValue": "TimeoutException"}},
                                    {"key": "url.full", "value": {"stringValue": "https://x/shop/123?token=secret"}},
                                    {"key": "db.statement", "value": {"stringValue": "select * from user where id=123"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    spans = _extract_trace_spans(payload)

    assert spans[0]["http_route"] == "/shop/{id}"
    assert spans[0]["http_method"] == "GET"
    assert spans[0]["http_status_code"] == 500
    assert spans[0]["error_type"] == "TimeoutException"
    assert "url.full" not in spans[0]["attributes"]
    assert "db.statement" not in spans[0]["attributes"]


async def test_mcp_registry_marks_transport_for_persistence(monkeypatch):
    """真实 MCP 适配层必须留下 transport=mcp，避免线上路径与本地 Fake 混淆。"""
    registry = MCPToolRegistry()

    async def fake_call_tool(name, arguments):
        assert name == "get_service_health"
        assert arguments["health_url"].endswith("/actuator/health")
        return {
            "tool_call_id": "mcp-tool-001",
            "source": "health",
            "status": "success",
            "query": arguments["health_url"],
            "time_range": {},
            "observed_at": datetime.now(UTC),
            "data": {"state": "UP"},
            "summary": "service health=UP",
            "error": "",
            "truncated": False,
        }

    monkeypatch.setattr(registry.client, "call_tool", fake_call_tool)
    result, record = await registry.execute(
        "get_service_health", health_url="http://merchantflow:8081/actuator/health"
    )

    assert result.status == "success"
    assert record["transport"] == "mcp"


async def test_thread_snapshot_uses_aksk_signature_without_exposing_secret(monkeypatch):
    """线程快照必须走固定内部地址和 HMAC 签名，返回内容不能携带 SK。"""
    client = ObservabilityClient()
    captured = {}
    monkeypatch.setattr("app.observability.client.config.merchantflow_ops_url", "http://merchantflow:8081")
    monkeypatch.setattr("app.observability.client.config.aksk_access_key", "test-ak")
    monkeypatch.setattr("app.observability.client.config.aksk_secret_key", "test-secret")

    async def fake_get(url, params=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {
            "success": True,
            "data": {
                "sample_interval_ms": 200,
                "threads": [{"name": "http-nio-8081-exec-1", "cpu_delta_nanos": 100}],
            },
        }

    monkeypatch.setattr(client, "_get", fake_get)
    result = await client.get_jvm_thread_snapshot("merchantflow")

    headers = captured["headers"]
    raw = (
        f"{headers['X-AK']}\n{headers['X-Timestamp']}\n{headers['X-Nonce']}\n"
        "GET\n/internal/ops/thread-snapshot"
    )
    expected = hmac.new(b"test-secret", raw.encode(), hashlib.sha256).hexdigest()
    assert captured["url"] == "http://merchantflow:8081/internal/ops/thread-snapshot"
    assert headers["X-Signature"] == expected
    assert result.status == "success"
    assert "test-secret" not in str(result.model_dump(mode="json"))
