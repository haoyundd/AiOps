"""Evidence-driven LangGraph investigation workflow.

The graph ranks hypotheses only from tool results. It deliberately returns
INCONCLUSIVE when independent evidence is missing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.config import config
from app.db import new_id
from app.domain import DiagnosisStatus, EvidenceStatus
from app.observability.tools import (
    MCPToolRegistry,
    ToolRegistry,
    mcp_ops_registry,
    ops_tool_registry,
)
from app.services.runbook_service import runbook_service


class InvestigationState(TypedDict, total=False):
    incident: dict[str, Any]
    service: dict[str, Any]
    tool_results: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    conclusion: dict[str, Any]
    # 记录实际经过的 LangGraph 节点，Worker 会据此持久化真实步骤数。
    # 下一步：诊断详情接口可直接展示这些节点，避免把固定节点数量当成运行事实。
    executed_steps: list[str]


def _number(value: Any) -> float | None:
    """将指标或 Trace 返回的字符串数值转成可比较的浮点数。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _span_dependency(span: dict[str, Any]) -> str:
    """根据 Span 属性识别下游依赖，为 Redis/MySQL/RocketMQ 分类提供输入。"""
    attributes = span.get("attributes") or {}
    values = " ".join(
        str(attributes.get(key, ""))
        for key in (
            "db.system",
            "db.operation.name",
            "messaging.system",
            "net.sock.peer.name",
            "server.address",
        )
    ).lower()
    values = f"{values} {span.get('name', '')} {span.get('scope', '')}".lower()
    for dependency in ("redis", "mysql", "rocketmq"):
        if dependency in values:
            return dependency
    return ""


def _span_is_slow_or_failed(span: dict[str, Any]) -> bool:
    """把明显超时或错误的子 Span 标记为依赖故障证据。"""
    duration = _number(span.get("duration_ms")) or 0
    state = str(span.get("status", "")).upper()
    return duration >= 1000 or "ERROR" in state


def _incident_observation_window(incident: dict[str, Any]) -> tuple[str, str]:
    """生成跨 MCP 进程可传输的告警观察窗口，并有限回溯到 firing 前的首条信号。"""
    end = datetime.now(UTC)
    # Alertmanager 的 fingerprint 可能跨多次 firing/resolved 复用，数据库 started_at 可能是旧轮次。
    # 优先读取本次 webhook 覆盖的 raw_alert.startsAt，避免旧 Incident 污染新一轮诊断。
    raw_alert = incident.get("raw_alert") or {}
    raw_start = raw_alert.get("startsAt") or incident.get("started_at")
    if isinstance(raw_start, datetime):
        start = raw_start if raw_start.tzinfo else raw_start.replace(tzinfo=UTC)
    elif isinstance(raw_start, str):
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except ValueError:
            start = end - timedelta(minutes=config.max_query_range_minutes)
    else:
        start = end - timedelta(minutes=config.max_query_range_minutes)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    # 指标规则存在 evaluation interval 与 for 窗口：异常日志往往先于 startsAt 出现。
    # 回溯受配置上限保护，下一步仍由 max_query_range_minutes 限制总查询范围。
    start -= timedelta(seconds=config.diagnosis_pre_alert_lookback_seconds)
    maximum = timedelta(minutes=config.max_query_range_minutes)
    if start > end or end - start > maximum:
        start = end - maximum
    return start.isoformat(), end.isoformat()


class EvidenceDiagnosisGraph:
    def __init__(self, registry: ToolRegistry | MCPToolRegistry | None = None) -> None:
        # 测试可传入 Fake Registry；生产默认走 MCP，MCP 失败时保留 unavailable 证据。
        self.registry = registry or (mcp_ops_registry if config.mcp_ops_enabled else ops_tool_registry)

    async def _gather_evidence(self, state: InvestigationState) -> dict[str, Any]:
        """并行采集健康、指标、日志和 Trace，并为慢 Trace 补充详情。"""
        incident = state["incident"]
        service = state["service"]
        service_name = str(
            (service.get("prometheus_labels") or {}).get("job") or incident["service_name"]
        )
        log_service_name = str(
            (service.get("loki_labels") or {}).get("service_name") or incident["service_name"]
        )
        trace_service_name = str(service.get("tempo_service_name") or incident["service_name"])
        health_url = str(service.get("health_url") or config.merchantflow_health_url)
        observation_start, observation_end = _incident_observation_window(incident)

        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_service_health", {"health_url": health_url}),
            ("get_red_metrics", {"service_name": service_name}),
            ("get_jvm_metrics", {"service_name": service_name}),
            ("get_messaging_metrics", {"service_name": service_name}),
            ("get_top_endpoint_metrics", {"service_name": service_name, "limit": 10}),
            (
                "get_log_error_patterns",
                {
                    "service_name": log_service_name,
                    "start": observation_start,
                    "end": observation_end,
                },
            ),
            (
                "search_tempo_traces",
                {
                    "service_name": trace_service_name,
                    "limit": 20,
                    "start": observation_start,
                    "end": observation_end,
                },
            ),
        ]
        executed = await asyncio.gather(
            *(self.registry.execute(name, **arguments) for name, arguments in calls)
        )
        results = [result.model_dump(mode="json") for result, _record in executed]
        records = [record for _result, record in executed]

        # 线程快照有额外采样开销，只在真实 JVM CPU 达到阈值后调用。
        jvm_result = next(
            (
                item
                for item in results
                if item.get("source") == "prometheus"
                and "process_cpu_usage" in (item.get("data") or {})
            ),
            None,
        )
        if (_number((jvm_result or {}).get("data", {}).get("process_cpu_usage")) or 0) >= 0.8:
            snapshot_result, snapshot_record = await self.registry.execute(
                "get_jvm_thread_snapshot", service_name=incident["service_name"]
            )
            results.append(snapshot_result.model_dump(mode="json"))
            records.append(snapshot_record)

        # 先搜索 Trace 摘要，再展开最慢的前五条，提取 Redis/MySQL/RocketMQ/HTTP 子 Span。
        trace_result = next(
            (item for item in results if item.get("source") == "tempo"), None
        )
        trace_rows = (trace_result or {}).get("data", {}).get("traces", [])
        # 下一步：对这些 Trace ID 调用 get_trace_detail，继续定位具体依赖子 Span。
        trace_ids = [
            str(item.get("traceID") or item.get("traceId") or "")
            for item in trace_rows
            if item.get("traceID") or item.get("traceId")
        ]
        trace_ids = trace_ids[:5]
        if trace_ids:
            detail_results = await asyncio.gather(
                *(self.registry.execute("get_trace_detail", trace_id=trace_id) for trace_id in trace_ids)
            )
            results.extend(result.model_dump(mode="json") for result, _record in detail_results)
            records.extend(record for _result, record in detail_results)

        runbooks = await runbook_service.search(
            incident["service_name"],
            f"{incident['alert_name']} {incident.get('description', '')}",
            limit=config.rag_top_k,
        )
        results.append(
            {
                "tool_call_id": new_id(),
                "source": "runbook",
                "status": "success",
                "query": incident["alert_name"],
                "time_range": {},
                "observed_at": incident["updated_at"],
                "data": {"matches": runbooks},
                "summary": f"Runbook returned {len(runbooks)} matching chunks",
                "error": "",
                "truncated": False,
            }
        )
        records.append(
            {
                "tool_name": "search_runbooks",
                "source": "runbook",
                "risk_level": "READ_ONLY",
                "status": "success",
                "input": {"service_name": incident["service_name"]},
                "output": {"matches": runbooks},
                "error": "",
                "duration_ms": 0,
            }
        )
        return {
            "tool_results": results,
            "tool_calls": records,
            "executed_steps": [*state.get("executed_steps", []), "gather_evidence"],
        }

    async def _normalize_evidence(self, state: InvestigationState) -> dict[str, Any]:
        """把不同观测工具的结果转换成统一的 EvidenceItem 结构。"""
        evidence: list[dict[str, Any]] = []
        for result in state["tool_results"]:
            source = result["source"]
            data = result.get("data") or {}
            status = EvidenceStatus.NEUTRAL
            kind = {
                "health": "service_health",
                "loki": "error_log_patterns",
                "tempo": "trace_summaries",
                "runbook": "runbook_context",
                "jvm": "jvm_thread_snapshot",
            }.get(source, source)
            if result["status"] != "success":
                status = EvidenceStatus.UNAVAILABLE
            elif source == "health":
                if str(data.get("state", "UNKNOWN")).upper() != "UP":
                    status = EvidenceStatus.SUPPORTED
            elif source == "prometheus":
                if "endpoints" in data:
                    kind = "endpoint_metrics"
                    endpoints = data.get("endpoints") or []
                    if any(
                        (_number(item.get("latency_p95_seconds")) or 0) >= 1.0
                        or (_number(item.get("error_rate")) or 0) >= 0.05
                        for item in endpoints
                    ):
                        status = EvidenceStatus.SUPPORTED
                elif "mq_publish_failures_5m" in data:
                    kind = "messaging_metrics"
                    if (_number(data.get("mq_publish_failures_5m")) or 0) > 0:
                        status = EvidenceStatus.SUPPORTED
                elif "process_cpu_usage" in data:
                    kind = "jvm_process_metrics"
                    if (_number(data.get("process_cpu_usage")) or 0) >= 0.8:
                        status = EvidenceStatus.SUPPORTED
                else:
                    kind = "red_metrics"
                    if (_number(data.get("latency_p95_seconds")) or 0) >= 1.0 or (
                        _number(data.get("error_rate")) or 0
                    ) >= 0.05:
                        status = EvidenceStatus.SUPPORTED
            elif source == "loki":
                patterns = data.get("patterns") or {}
                if sum(int(value or 0) for value in patterns.values()) > 0:
                    status = EvidenceStatus.SUPPORTED
            elif source == "tempo":
                if "spans" in data:
                    kind = "trace_details"
                    spans = data.get("spans") or []
                    if any((_number(span.get("duration_ms")) or 0) >= 1000 for span in spans):
                        status = EvidenceStatus.SUPPORTED
                else:
                    traces = data.get("traces") or []
                    if any((_number(trace.get("durationMs")) or 0) >= 1000 for trace in traces):
                        status = EvidenceStatus.SUPPORTED
            elif source == "runbook":
                kind = "runbook_context"
            elif source == "jvm":
                kind = "jvm_thread_snapshot"
                if data.get("threads"):
                    status = EvidenceStatus.SUPPORTED
            if source == "tempo" and "spans" in data:
                # 子 Span 是原始诊断事实，只有慢或错误 Span 才支持依赖类根因。
                if any(_span_is_slow_or_failed(span) for span in data.get("spans", [])):
                    status = EvidenceStatus.SUPPORTED
            evidence.append(
                {
                    "id": new_id(),
                    "hypothesis_id": None,
                    "source": source,
                    "kind": kind,
                    "status": status.value,
                    "summary": result.get("summary") or result.get("error") or "no summary",
                    "query": result.get("query", ""),
                    "data": data,
                }
            )
        return {
            "evidence": evidence,
            "executed_steps": [*state.get("executed_steps", []), "normalize_evidence"],
        }

    async def _rank_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        """基于独立证据类型计算六类根因候选并排序。"""
        # 同一种 kind 可能有多个 Trace 详情，必须聚合而不是用字典覆盖最后一条。
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in state["evidence"]:
            by_kind.setdefault(item["kind"], []).append(item)
        health = by_kind.get("service_health", [{}])[0]
        red = by_kind.get("red_metrics", [{}])[0]
        jvm = by_kind.get("jvm_process_metrics", [{}])[0]
        messaging = by_kind.get("messaging_metrics", [{}])[0]
        endpoint_metrics = by_kind.get("endpoint_metrics", [{}])[0]
        thread_snapshot = by_kind.get("jvm_thread_snapshot", [{}])[0]
        logs = by_kind.get("error_log_patterns", [{}])[0]
        traces = by_kind.get("trace_summaries", [{}])[0]
        trace_details = by_kind.get("trace_details", [])

        health_data = health.get("data") or {}
        health_components = (health_data.get("details") or {}).get("components") or {}
        red_data = red.get("data") or {}
        jvm_data = jvm.get("data") or {}
        messaging_data = messaging.get("data") or {}
        endpoint_rows = (endpoint_metrics.get("data") or {}).get("endpoints") or []
        log_patterns = (logs.get("data") or {}).get("patterns") or {}
        trace_rows = (traces.get("data") or {}).get("traces") or []
        trace_spans = [
            span
            for evidence in trace_details
            for span in (evidence.get("data") or {}).get("spans", [])
        ]
        # 候选列表由六类根因逐项填充，最后按置信度排序并取前三名。
        candidates: list[dict[str, Any]] = []
        p95 = _number(red_data.get("latency_p95_seconds")) or 0
        error_rate = _number(red_data.get("error_rate")) or 0
        cpu = _number(jvm_data.get("process_cpu_usage")) or 0
        mq_publish_failures = _number(messaging_data.get("mq_publish_failures_5m")) or 0
        endpoint_hot = any(
            (_number(item.get("request_rate")) or 0) >= 5.0
            or (_number(item.get("latency_p95_seconds")) or 0) >= 1.0
            for item in endpoint_rows
        )
        health_available = bool(health) and health.get("status") != EvidenceStatus.UNAVAILABLE.value
        health_up = health_available and str(health_data.get("state", "UNKNOWN")).upper() == "UP"
        logs_available = bool(logs) and logs.get("status") != EvidenceStatus.UNAVAILABLE.value
        slow_trace = any((_number(row.get("durationMs")) or 0) >= 1000 for row in trace_rows) or any(
            _span_is_slow_or_failed(row) for row in trace_spans
        )

        def component_down(name: str) -> bool:
            """读取 Actuator 组件状态，避免把整体 UP 误判成依赖一定正常。"""
            component = health_components.get(name) or {}
            return str(component.get("status", "UNKNOWN")).upper() != "UP"

        def dependency_spans(name: str) -> list[dict[str, Any]]:
            """筛选某个依赖的所有子 Span，用于区分 Redis 和 MySQL。"""
            return [span for span in trace_spans if _span_dependency(span) == name]

        def dependency_signal(name: str) -> bool:
            """判断依赖 Span 是否出现慢或错误信号。"""
            return any(_span_is_slow_or_failed(span) for span in dependency_spans(name))

        def add_candidate(
            category: str,
            title: str,
            score: float,
            support: list[dict[str, Any]],
            contradict: list[dict[str, Any]],
        ) -> None:
            """记录候选根因；少于两个独立证据类型时强制降为不可定案。"""
            valid_support = [
                item for item in support if item and item.get("status") == EvidenceStatus.SUPPORTED.value
            ]
            # 同一个 Prometheus 可以同时提供 CPU 和 RED，但二者是独立信号，必须分别计算。
            evidence_kind_count = len({item["kind"] for item in valid_support})
            confidence = min(max(score, 0.0), 0.99)
            if evidence_kind_count < 2:
                confidence = min(confidence, 0.64)
            candidates.append(
                {
                    "id": new_id(),
                    "rank": 0,
                    "category": category,
                    "title": title,
                    "confidence": round(confidence, 2),
                    "verdict": "SUPPORTED"
                    if confidence >= 0.55 and evidence_kind_count >= 2
                    else "UNCONFIRMED",
                    "supporting_evidence_ids": [item["id"] for item in valid_support],
                    "contradicting_evidence_ids": [item["id"] for item in contradict if item],
                }
            )

        redis_logs = int(log_patterns.get("redis") or 0)
        mysql_logs = int(log_patterns.get("mysql") or 0)
        rocketmq_logs = int(log_patterns.get("rocketmq") or 0)
        timeout_logs = int(log_patterns.get("timeout") or 0)
        refused_logs = int(log_patterns.get("connection_refused") or 0)
        app_errors = int(log_patterns.get("application_error") or 0)

        # 全局 HTTP P95 只能证明“请求变慢”，不能单独指向 Redis；必须同时看到
        # Redis 子 Span、Redis 日志或超时日志。下一步：相同原则也用于 MySQL 子 Span，
        # 防止一个依赖的慢请求被错误归因到另一个依赖。
        redis_latency_signal = dependency_signal("redis") or redis_logs > 0 or timeout_logs > 0
        mysql_latency_signal = dependency_signal("mysql") or mysql_logs > 0 or timeout_logs > 0

        # 每个候选都明确绑定独立观测源；下一步由 build_report 根据最高分生成结论。
        add_candidate(
            "REDIS_OUTAGE",
            "Redis dependency is unavailable",
            (0.5 if component_down("redis") else 0)
            + (0.3 if redis_logs and refused_logs else 0)
            + (0.2 if error_rate >= 0.05 else 0),
            [health if component_down("redis") else {}, logs if redis_logs else {}, red if error_rate >= 0.05 else {}],
            [health if health_up else {}],
        )
        add_candidate(
            "MYSQL_OUTAGE",
            "MySQL dependency is slow or unavailable",
            (0.5 if component_down("db") else 0)
            + (0.3 if mysql_logs and refused_logs else 0)
            + (0.45 if dependency_signal("mysql") and p95 >= 1.0 else 0)
            + (0.15 if mysql_latency_signal and p95 >= 1.0 else 0)
            + (0.05 if health_up and mysql_latency_signal else 0)
            + (0.2 if error_rate >= 0.05 else 0),
            [
                health if component_down("db") else {},
                logs if mysql_logs else {},
                trace_details[0] if dependency_signal("mysql") else {},
                red if error_rate >= 0.05 or (mysql_latency_signal and p95 >= 1.0) else {},
            ],
            [health if health_up else {}],
        )
        add_candidate(
            "ROCKETMQ_FAILURE",
            "RocketMQ message delivery is failing",
            (0.55 if mq_publish_failures > 0 else 0)
            + (0.35 if rocketmq_logs else 0)
            + (0.3 if any(_span_is_slow_or_failed(span) for span in dependency_spans("rocketmq")) else 0)
            + (0.1 if error_rate >= 0.05 else 0),
            [
                messaging if mq_publish_failures > 0 else {},
                logs if rocketmq_logs else {},
                trace_details[0]
                if any(_span_is_slow_or_failed(span) for span in dependency_spans("rocketmq"))
                and trace_details
                else {},
                red if error_rate >= 0.05 else {},
            ],
            [],
        )
        add_candidate(
            "REDIS_LATENCY",
            "Redis dependency is slow or timing out",
            (0.4 if p95 >= 1.0 and redis_latency_signal else 0)
            + (0.35 if dependency_signal("redis") else 0)
            + (0.2 if redis_logs and timeout_logs else 0)
            + (0.05 if health_up else 0),
            [
                red if p95 >= 1.0 and redis_latency_signal else {},
                trace_details[0] if dependency_signal("redis") and trace_details else {},
                logs if redis_logs and timeout_logs else {},
            ],
            [health if component_down("redis") else {}],
        )
        add_candidate(
            "CPU_SATURATION",
            "MerchantFlow process CPU is saturated",
            (0.55 if cpu >= 0.8 else 0)
            + (0.2 if p95 >= 1.0 else 0)
            + (0.15 if error_rate >= 0.05 else 0)
            + (0.1 if endpoint_hot else 0)
            + (0.15 if thread_snapshot.get("status") == EvidenceStatus.SUPPORTED.value else 0)
            + (0.1 if logs_available and app_errors and not (redis_logs or mysql_logs or rocketmq_logs) else 0),
            [
                jvm if cpu >= 0.8 else {},
                red if p95 >= 1.0 else {},
                endpoint_metrics if endpoint_hot else {},
                thread_snapshot,
                logs if app_errors else {},
            ],
            [logs if redis_logs or mysql_logs or rocketmq_logs else {}],
        )
        add_candidate(
            "APPLICATION_ERROR",
            "MerchantFlow application is returning errors",
            (0.45 if error_rate >= 0.05 else 0)
            + (0.35 if app_errors else 0)
            + (0.2 if slow_trace and not (redis_logs or mysql_logs or rocketmq_logs) else 0),
            [red if error_rate >= 0.05 else {}, logs if app_errors else {}, traces if slow_trace else {}],
            [logs if redis_logs or mysql_logs or rocketmq_logs else {}],
        )

        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return {
            "hypotheses": candidates[:3],
            "executed_steps": [*state.get("executed_steps", []), "rank_hypotheses"],
        }

    async def _build_report(self, state: InvestigationState) -> dict[str, Any]:
        """根据最高置信度候选生成结论；证据不足时明确返回 INCONCLUSIVE。"""
        hypotheses = state["hypotheses"]
        top = hypotheses[0]
        available_sources = {
            item["source"]
            for item in state["evidence"]
            if item["status"] != EvidenceStatus.UNAVAILABLE.value
        }
        unavailable_sources = sorted(
            {
                item["source"]
                for item in state["evidence"]
                if item["status"] == EvidenceStatus.UNAVAILABLE.value
            }
        )
        # 下一步：Worker 会把 DIAGNOSED 结论送入 Runbook Draft 审核流。
        conclusive = top["confidence"] >= 0.65 and len(available_sources) >= 2
        root_cause = top["title"] if conclusive else "Evidence is insufficient"
        endpoint_evidence = next(
            (item for item in state["evidence"] if item["kind"] == "endpoint_metrics"), {}
        )
        endpoint_rows = (endpoint_evidence.get("data") or {}).get("endpoints") or []
        trace_spans = [
            span
            for item in state["evidence"]
            if item["kind"] == "trace_details"
            for span in (item.get("data") or {}).get("spans", [])
        ]
        affected_endpoints: list[dict[str, Any]] = []
        if conclusive:
            for endpoint in endpoint_rows:
                method = str(endpoint.get("method") or "")
                route = str(endpoint.get("uri") or "")
                linked = [
                    span
                    for span in trace_spans
                    if span.get("http_method") == method and span.get("http_route") == route
                ]
                abnormal = (
                    (_number(endpoint.get("latency_p95_seconds")) or 0) >= 1.0
                    or (_number(endpoint.get("error_rate")) or 0) >= 0.05
                    or any(_span_is_slow_or_failed(span) for span in linked)
                )
                if not abnormal and top["category"] != "CPU_SATURATION":
                    continue
                affected_endpoints.append(
                    {
                        **endpoint,
                        "trace_ids": sorted(
                            {str(span.get("trace_id")) for span in linked if span.get("trace_id")}
                        )[:5],
                    }
                )
                if len(affected_endpoints) >= 5:
                    break
        recommendations = {
            "CPU_SATURATION": [
                "Confirm the hot endpoint and JVM thread activity before scaling",
                "Reduce load or increase the container CPU quota in a controlled rollout",
            ],
            "REDIS_LATENCY": [
                "Inspect the slow Redis child span and Redis connection pool saturation",
                "Remove the lab proxy latency only after operator approval",
            ],
            "REDIS_OUTAGE": [
                "Verify Redis reachability and credentials from the application network",
                "Restore the Redis proxy route only after operator approval",
            ],
            "MYSQL_OUTAGE": [
                "Verify MySQL reachability, credentials, and connection pool health",
                "Restore the database route only after operator approval",
            ],
            "ROCKETMQ_FAILURE": [
                "Verify RocketMQ NameServer and broker reachability",
                "Inspect failed message retries before replaying any message",
            ],
            "APPLICATION_ERROR": [
                "Inspect application exception logs and the failing HTTP trace",
                "Roll back or patch the faulty endpoint only through the normal release process",
            ],
        }.get(top["category"], ["Collect additional metrics, logs, and traces"])
        status = DiagnosisStatus.DIAGNOSED if conclusive else DiagnosisStatus.INCONCLUSIVE
        if not conclusive:
            recommendations = [
                "Restore unavailable observability sources before drawing a root-cause conclusion",
                "Collect at least two independent signals from metrics, logs, traces, or health",
            ]
        summary = (
            f"Most likely root cause: {root_cause} (confidence {top['confidence']:.0%})"
            if conclusive
            else "The available evidence does not support a reliable root-cause conclusion"
        )
        hypothesis_lines = "\n".join(
            f"{item['rank']}. {item['title']} — {item['confidence']:.0%} ({item['verdict']})"
            for item in hypotheses
        )
        evidence_lines = "\n".join(
            f"- [{item['source']}/{item['kind']}] {item['summary']}"
            for item in state["evidence"]
        )
        endpoint_lines = (
            "\n".join(
                f"- {item['method']} {item['uri']}: rate={item.get('request_rate')}, "
                f"p95={item.get('latency_p95_seconds')}s, error_rate={item.get('error_rate')}"
                for item in affected_endpoints
            )
            or "- No route-level evidence was available"
        )
        report = (
            f"# Incident diagnosis\n\n## Conclusion\n{summary}\n\n"
            f"## Ranked hypotheses\n{hypothesis_lines}\n\n"
            f"## Affected endpoints\n{endpoint_lines}\n\n"
            f"## Evidence\n{evidence_lines}\n\n"
            f"## Recommended next steps\n"
            + "\n".join(f"- {item}" for item in recommendations)
        )
        return {
            "conclusion": {
                "status": status.value,
                "root_cause": root_cause,
                "category": top["category"] if conclusive else "INCONCLUSIVE",
                "confidence": top["confidence"],
                "summary": summary,
                "recommendations": recommendations,
                "affected_endpoints": affected_endpoints,
                "unavailable_sources": unavailable_sources,
                "report_markdown": report,
            },
            "executed_steps": [*state.get("executed_steps", []), "build_report"],
        }

    def _compile(self, checkpointer: Any) -> Any:
        """编译 LangGraph；生产使用 PostgreSQL 检查点，测试可使用内存检查点。"""
        workflow = StateGraph(InvestigationState)
        workflow.add_node("gather_evidence", self._gather_evidence)
        workflow.add_node("normalize_evidence", self._normalize_evidence)
        workflow.add_node("rank_hypotheses", self._rank_hypotheses)
        workflow.add_node("build_report", self._build_report)
        workflow.add_edge(START, "gather_evidence")
        workflow.add_edge("gather_evidence", "normalize_evidence")
        workflow.add_edge("normalize_evidence", "rank_hypotheses")
        workflow.add_edge("rank_hypotheses", "build_report")
        workflow.add_edge("build_report", END)
        return workflow.compile(checkpointer=checkpointer)

    async def run(
        self,
        *,
        run_id: str,
        incident: dict[str, Any],
        service: dict[str, Any],
    ) -> InvestigationState:
        """执行一次完整诊断，下一步由 Worker 持久化结论并生成知识草稿。"""
        initial: InvestigationState = {"incident": incident, "service": service}
        graph_config = {"configurable": {"thread_id": run_id}}
        if config.is_postgres:
            checkpoint_url = config.database_url.replace("postgresql+asyncpg://", "postgresql://")
            async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as saver:
                await saver.setup()
                graph = self._compile(saver)
                return cast(
                    InvestigationState, await graph.ainvoke(initial, config=graph_config)
                )
        graph = self._compile(MemorySaver())
        return cast(InvestigationState, await graph.ainvoke(initial, config=graph_config))


evidence_diagnosis_graph = EvidenceDiagnosisGraph()
