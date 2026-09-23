from datetime import UTC, datetime, timedelta

from app.agent.evidence_graph import EvidenceDiagnosisGraph, _incident_observation_window
from app.config import config
from app.schemas import ToolResult


def result(source: str, data: dict, status: str = "success") -> ToolResult:
    return ToolResult(
        tool_call_id=f"{source}-call",
        source=source,
        status=status,
        observed_at=datetime.now(UTC),
        data=data,
        summary=f"{source} evidence",
        error="source unavailable" if status != "success" else "",
    )


class FakeRegistry:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = []

    async def execute(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if self.mode == "unavailable":
            item = result(name.split("_")[1], {}, "unavailable")
        elif name == "get_service_health":
            item = result("health", {"state": "UP", "latency_ms": 20})
        elif name == "get_red_metrics":
            item = result(
                "prometheus",
                {"request_rate": 20.0, "error_rate": 0.02, "latency_p95_seconds": 1.4},
            )
        elif name == "get_jvm_metrics":
            item = result(
                "prometheus",
                {"process_cpu_usage": 0.93, "heap_used_bytes": 1000, "live_threads": 40},
            )
        elif name == "get_messaging_metrics":
            item = result(
                "prometheus",
                {"mq_publish_failures_5m": 0.0, "failures_by_flow": {}},
            )
        elif name == "get_top_endpoint_metrics":
            item = result(
                "prometheus",
                {
                    "endpoints": [
                        {
                            "method": "GET",
                            "uri": "/shop/{id}",
                            "request_rate": 12.0,
                            "latency_p95_seconds": 1.4,
                            "error_rate": 0.0,
                        }
                    ]
                },
            )
        elif name == "get_jvm_thread_snapshot":
            item = result(
                "jvm",
                {
                    "sample_interval_ms": 200,
                    "threads": [{"name": "hot-loop", "cpu_delta_nanos": 1000000}],
                },
            )
        elif name == "get_log_error_patterns":
            item = result(
                "loki",
                {"patterns": {"redis": 0, "mysql": 0, "rocketmq": 0, "timeout": 0}},
            )
        else:
            item = result("tempo", {"traces": []})
        record = {
            "tool_name": name,
            "source": item.source,
            "risk_level": "READ_ONLY",
            "status": item.status,
            "input": kwargs,
            "output": item.model_dump(mode="json"),
            "error": item.error,
            "duration_ms": 1,
        }
        return item, record


def inputs():
    return {
        "run_id": "graph-run-001",
        "incident": {
            "id": "incident-001",
            "service_name": "merchantflow",
            "environment": "test",
            "alert_name": "MerchantFlowHighCPU",
            "description": "CPU is high",
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "service": {
            "prometheus_labels": {"job": "merchantflow"},
            "loki_labels": {"service_name": "merchantflow"},
            "tempo_service_name": "merchantflow",
            "health_url": "http://merchantflow/actuator/health",
        },
    }


async def test_graph_diagnoses_cpu_from_real_signal_shapes():
    output = await EvidenceDiagnosisGraph(FakeRegistry("cpu")).run(**inputs())
    assert output["conclusion"]["status"] == "DIAGNOSED"
    assert output["conclusion"]["category"] == "CPU_SATURATION"
    assert output["hypotheses"][0]["confidence"] >= 0.65
    assert output["conclusion"]["affected_endpoints"][0]["uri"] == "/shop/{id}"
    assert all(call["risk_level"] == "READ_ONLY" for call in output["tool_calls"])
    assert any(call["tool_name"] == "get_jvm_thread_snapshot" for call in output["tool_calls"])


async def test_graph_returns_inconclusive_when_sources_are_unavailable():
    values = inputs()
    values["run_id"] = "graph-run-002"
    output = await EvidenceDiagnosisGraph(FakeRegistry("unavailable")).run(**values)
    assert output["conclusion"]["status"] == "INCONCLUSIVE"
    assert output["conclusion"]["root_cause"] == "Evidence is insufficient"


async def test_graph_passes_incident_window_to_log_and_trace_tools():
    """日志和 Trace 必须携带告警开始时间，防止旧故障污染本次诊断。"""
    registry = FakeRegistry("cpu")
    values = inputs()
    values["run_id"] = "graph-run-window"
    values["incident"]["started_at"] = "2020-01-01T00:00:00+00:00"
    values["incident"]["raw_alert"] = {"startsAt": datetime.now(UTC).isoformat()}

    await EvidenceDiagnosisGraph(registry).run(**values)

    calls = dict(registry.calls)
    assert calls["get_log_error_patterns"]["start"]
    assert calls["get_log_error_patterns"]["end"]
    assert calls["search_tempo_traces"]["start"]
    assert calls["search_tempo_traces"]["end"]
    assert calls["get_messaging_metrics"]["service_name"] == "merchantflow"
    assert calls["get_top_endpoint_metrics"]["limit"] == 10
    assert calls["get_log_error_patterns"]["start"].startswith("2026-")


def test_observation_window_includes_bounded_pre_alert_evidence():
    """firing 前的异常日志必须可见，但回溯窗口不能无限扩大为历史检索。"""
    # 使用当前窗口内的告警时间，避免触发另一条“超出最大查询范围时裁剪”的安全分支。
    alert_start = datetime.now(UTC) - timedelta(seconds=30)
    start, end = _incident_observation_window(
        {"raw_alert": {"startsAt": alert_start.isoformat()}}
    )

    start_at = datetime.fromisoformat(start)
    end_at = datetime.fromisoformat(end)
    assert start_at == alert_start - timedelta(
        seconds=config.diagnosis_pre_alert_lookback_seconds
    )
    assert end_at >= alert_start
    assert end_at - start_at <= timedelta(minutes=config.max_query_range_minutes)
