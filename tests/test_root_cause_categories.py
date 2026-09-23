from datetime import UTC, datetime

import pytest

from app.agent.evidence_graph import EvidenceDiagnosisGraph
from app.schemas import ToolResult


def _result(source: str, data: dict) -> ToolResult:
    """构造与 MCP/本地 Registry 相同的结构化工具结果。"""
    return ToolResult(
        tool_call_id=f"{source}-case",
        source=source,
        status="success",
        observed_at=datetime.now(UTC),
        data=data,
        summary=f"{source} test evidence",
    )


class CategoryRegistry:
    """按故障类型返回确定性证据，用于隔离验证根因排序规则。"""

    def __init__(self, category: str) -> None:
        self.category = category

    async def execute(self, name: str, **kwargs):
        category = self.category
        components = {"redis": {"status": "UP"}, "db": {"status": "UP"}}
        state = "UP"
        if category == "REDIS_OUTAGE":
            state = "DOWN"
            components["redis"] = {"status": "DOWN"}
        if category == "MYSQL_OUTAGE":
            state = "DOWN"
            components["db"] = {"status": "DOWN"}
        if name == "get_service_health":
            item = _result(
                "health",
                {"state": state, "details": {"components": components}},
            )
        elif name == "get_red_metrics":
            item = _result(
                "prometheus",
                {
                    "request_rate": 20.0,
                    "error_rate": 0.1 if category in {"ROCKETMQ_FAILURE", "APPLICATION_ERROR"} else 0.02,
                    "latency_p95_seconds": 1.8 if category != "APPLICATION_ERROR" else 0.4,
                },
            )
        elif name == "get_jvm_metrics":
            item = _result(
                "prometheus",
                {"process_cpu_usage": 0.93 if category == "CPU_SATURATION" else 0.2},
            )
        elif name == "get_messaging_metrics":
            failures = 2.0 if category == "ROCKETMQ_FAILURE" else 0.0
            item = _result(
                "prometheus",
                {
                    "mq_publish_failures_5m": failures,
                    "failures_by_flow": {"seckill-order": failures} if failures else {},
                },
            )
        elif name == "get_top_endpoint_metrics":
            item = _result(
                "prometheus",
                {
                    "endpoints": [
                        {
                            "method": "POST",
                            "uri": "/voucher-order/seckill/{id}",
                            "request_rate": 10.0,
                            "latency_p95_seconds": 1.8,
                            "error_rate": 0.1 if category == "APPLICATION_ERROR" else 0.0,
                        }
                    ]
                },
            )
        elif name == "get_jvm_thread_snapshot":
            item = _result(
                "jvm",
                {"threads": [{"name": "hot-loop", "cpu_delta_nanos": 9000000}]},
            )
        elif name == "get_log_error_patterns":
            item = _result(
                "loki",
                {
                    "patterns": {
                        "redis": 2 if category.startswith("REDIS") else 0,
                        "mysql": 2 if category.startswith("MYSQL") else 0,
                        "rocketmq": 2 if category == "ROCKETMQ_FAILURE" else 0,
                        "timeout": 2 if category == "REDIS_LATENCY" else 0,
                        "connection_refused": 2 if category in {"REDIS_OUTAGE", "MYSQL_OUTAGE"} else 0,
                        "application_error": 2 if category == "APPLICATION_ERROR" else 0,
                    }
                },
            )
        elif name == "search_tempo_traces":
            item = _result("tempo", {"traces": [{"traceID": "a" * 32, "durationMs": 1800}]})
        elif name == "get_trace_detail":
            dependency = category.split("_")[0].lower()
            spans = []
            if dependency in {"redis", "mysql", "rocketmq"}:
                spans = [
                    {
                        "duration_ms": 1800,
                        "name": "SendMessage" if dependency == "rocketmq" else "query",
                        "scope": dependency,
                        "status": "ERROR" if dependency == "rocketmq" else "UNSET",
                        "attributes": {"db.system": dependency},
                    }
                ]
            item = _result("tempo", {"trace_id": kwargs["trace_id"], "spans": spans})
        else:
            item = _result("tempo", {})
        return item, {
            "tool_name": name,
            "source": item.source,
            "risk_level": "READ_ONLY",
            "status": item.status,
            "input": kwargs,
            "output": item.model_dump(mode="json"),
            "error": "",
            "duration_ms": 1,
        }


class JvmOnlyRegistry(CategoryRegistry):
    """只提供 JVM CPU 异常，验证单一证据类型不能得出确定结论。"""

    async def execute(self, name: str, **kwargs):
        if name == "get_jvm_thread_snapshot":
            item = _result("jvm", {"threads": []})
            return item, {
                "tool_name": name,
                "source": item.source,
                "risk_level": "READ_ONLY",
                "status": item.status,
                "input": kwargs,
                "output": item.model_dump(mode="json"),
                "error": "",
                "duration_ms": 1,
            }
        if name == "get_top_endpoint_metrics":
            item = _result("prometheus", {"endpoints": []})
            return item, {
                "tool_name": name,
                "source": item.source,
                "risk_level": "READ_ONLY",
                "status": item.status,
                "input": kwargs,
                "output": item.model_dump(mode="json"),
                "error": "",
                "duration_ms": 1,
            }
        if name == "get_red_metrics":
            item = _result(
                "prometheus",
                {"request_rate": 20.0, "error_rate": 0.0, "latency_p95_seconds": 0.1},
            )
            return item, {
                "tool_name": name,
                "source": item.source,
                "risk_level": "READ_ONLY",
                "status": item.status,
                "input": kwargs,
                "output": item.model_dump(mode="json"),
                "error": "",
                "duration_ms": 1,
            }
        return await super().execute(name, **kwargs)


def _inputs(category: str) -> dict:
    """构造诊断图需要的 Incident、服务标签和内部健康地址。"""
    return {
        "run_id": f"category-{category.lower()}",
        "incident": {
            "id": f"incident-{category.lower()}",
            "service_name": "merchantflow",
            "environment": "test",
            "alert_name": f"MerchantFlow{category}",
            "description": "test fault",
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "service": {
            "prometheus_labels": {"job": "merchantflow"},
            "loki_labels": {"service_name": "merchantflow"},
            "tempo_service_name": "merchantflow",
            "health_url": "http://merchantflow/actuator/health",
        },
    }


@pytest.mark.parametrize(
    "category",
    [
        "CPU_SATURATION",
        "REDIS_LATENCY",
        "REDIS_OUTAGE",
        "MYSQL_OUTAGE",
        "ROCKETMQ_FAILURE",
        "APPLICATION_ERROR",
    ],
)
async def test_evidence_graph_classifies_six_root_causes(category: str):
    """六类故障必须由不同观测证据支持，而不是依赖告警名称猜测。"""
    output = await EvidenceDiagnosisGraph(CategoryRegistry(category)).run(
        **_inputs(category)
    )
    assert output["conclusion"]["status"] == "DIAGNOSED"
    assert output["conclusion"]["category"] == category


async def test_single_evidence_type_is_inconclusive():
    """只有 JVM CPU 一个异常信号时，不能直接认定 CPU 是根因。"""
    output = await EvidenceDiagnosisGraph(JvmOnlyRegistry("CPU_SATURATION")).run(
        **_inputs("CPU_SATURATION")
    )
    assert output["conclusion"]["status"] == "INCONCLUSIVE"


async def test_mysql_slow_span_is_not_misclassified_as_redis():
    """MySQL 慢子 Span 与 RED 指标同时出现时，不能被全局 P95 误判成 Redis。"""
    output = await EvidenceDiagnosisGraph(CategoryRegistry("MYSQL_LATENCY")).run(
        **_inputs("MYSQL_LATENCY")
    )
    assert output["conclusion"]["status"] == "DIAGNOSED"
    assert output["conclusion"]["category"] == "MYSQL_OUTAGE"
