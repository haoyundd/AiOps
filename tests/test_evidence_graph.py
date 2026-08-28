from datetime import UTC, datetime

from app.agent.evidence_graph import EvidenceDiagnosisGraph
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

    async def execute(self, name: str, **kwargs):
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
    assert all(call["risk_level"] == "READ_ONLY" for call in output["tool_calls"])


async def test_graph_returns_inconclusive_when_sources_are_unavailable():
    values = inputs()
    values["run_id"] = "graph-run-002"
    output = await EvidenceDiagnosisGraph(FakeRegistry("unavailable")).run(**values)
    assert output["conclusion"]["status"] == "INCONCLUSIVE"
    assert output["conclusion"]["root_cause"] == "Evidence is insufficient"
