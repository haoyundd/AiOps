from datetime import UTC, datetime, timedelta

import httpx

from app.observability.client import ObservabilityClient
from app.observability.tools import ToolDefinition, ToolRegistry
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

    async def fake_get(url, params=None):
        if "loki/api" in url:
            return {
                "data": {
                    "result": [
                        {
                            "stream": {"service_name": "merchantflow"},
                            "values": [["1", "Redis connection timeout and refused"]],
                        }
                    ]
                }
            }
        if url.endswith("/api/v1/query_range"):
            return {"data": {"result": [{"values": [["1", "0.9"]]}]}}
        if url.endswith("/api/v1/query"):
            query = params["query"]
            value = "0.93" if "process_cpu_usage" in query else "1.4"
            return {"data": {"result": [{"value": ["1", value]}]}}
        if url.endswith("/api/search"):
            return {"traces": [{"traceID": "a" * 32, "durationMs": 1450}]}
        if "/api/traces/" in url:
            return {"batches": [{"resource": {"service.name": "merchantflow"}}]}
        return {"status": "UP", "components": {"redis": {"status": "UP"}}}

    monkeypatch.setattr(client, "_get", fake_get)
    metric_range = await client.query_prometheus_range("process_cpu_usage")
    red = await client.get_red_metrics("merchantflow")
    jvm = await client.get_jvm_metrics("merchantflow")
    logs = await client.query_loki_logs("merchantflow", keyword="timeout", limit=5)
    patterns = await client.get_log_error_patterns("merchantflow")
    traces = await client.search_tempo_traces("merchantflow")
    trace = await client.get_trace_detail("a" * 32)
    health = await client.get_service_health("http://merchantflow/actuator/health")

    assert metric_range.status == "success"
    assert red.data["latency_p95_seconds"] == 1.4
    assert jvm.data["process_cpu_usage"] == 0.93
    assert logs.data["samples"][0]["line"].startswith("Redis")
    assert patterns.data["patterns"]["redis"] == 1
    assert traces.data["traces"][0]["durationMs"] == 1450
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
