"""Evidence-driven LangGraph investigation workflow.

The graph ranks hypotheses only from tool results. It deliberately returns
INCONCLUSIVE when independent evidence is missing.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.config import config
from app.db import new_id
from app.domain import DiagnosisStatus, EvidenceStatus
from app.observability.tools import ToolRegistry, ops_tool_registry
from app.services.runbook_service import runbook_service


class InvestigationState(TypedDict, total=False):
    incident: dict[str, Any]
    service: dict[str, Any]
    tool_results: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    conclusion: dict[str, Any]


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class EvidenceDiagnosisGraph:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ops_tool_registry

    async def _gather_evidence(self, state: InvestigationState) -> dict[str, Any]:
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

        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_service_health", {"health_url": health_url}),
            ("get_red_metrics", {"service_name": service_name}),
            ("get_jvm_metrics", {"service_name": service_name}),
            ("get_log_error_patterns", {"service_name": log_service_name}),
            ("search_tempo_traces", {"service_name": trace_service_name, "limit": 20}),
        ]
        executed = await asyncio.gather(
            *(self.registry.execute(name, **arguments) for name, arguments in calls)
        )
        results = [result.model_dump(mode="json") for result, _record in executed]
        records = [record for _result, record in executed]

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
        return {"tool_results": results, "tool_calls": records}

    async def _normalize_evidence(self, state: InvestigationState) -> dict[str, Any]:
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
            }.get(source, source)
            if result["status"] != "success":
                status = EvidenceStatus.UNAVAILABLE
            elif source == "health":
                if str(data.get("state", "UNKNOWN")).upper() != "UP":
                    status = EvidenceStatus.SUPPORTED
            elif source == "prometheus":
                if "process_cpu_usage" in data:
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
                traces = data.get("traces") or []
                if any((_number(trace.get("durationMs")) or 0) >= 1000 for trace in traces):
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
        return {"evidence": evidence}

    async def _rank_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        by_kind = {item["kind"]: item for item in state["evidence"]}
        health = by_kind.get("service_health", {})
        red = by_kind.get("red_metrics", {})
        jvm = by_kind.get("jvm_process_metrics", {})
        logs = by_kind.get("error_log_patterns", {})
        traces = by_kind.get("trace_summaries", {})

        health_data = health.get("data") or {}
        red_data = red.get("data") or {}
        jvm_data = jvm.get("data") or {}
        log_patterns = (logs.get("data") or {}).get("patterns") or {}
        trace_rows = (traces.get("data") or {}).get("traces") or []
        dependency_mentions = sum(
            int(log_patterns.get(name) or 0) for name in ("redis", "mysql", "rocketmq")
        )
        timeouts = int(log_patterns.get("timeout") or 0)
        refused = int(log_patterns.get("connection_refused") or 0)
        p95 = _number(red_data.get("latency_p95_seconds")) or 0
        error_rate = _number(red_data.get("error_rate")) or 0
        cpu = _number(jvm_data.get("process_cpu_usage")) or 0
        health_available = bool(health) and health.get("status") != EvidenceStatus.UNAVAILABLE.value
        health_down = health_available and str(health_data.get("state", "UNKNOWN")).upper() != "UP"
        health_up = health_available and str(health_data.get("state", "UNKNOWN")).upper() == "UP"
        logs_available = bool(logs) and logs.get("status") != EvidenceStatus.UNAVAILABLE.value
        slow_trace = any((_number(row.get("durationMs")) or 0) >= 1000 for row in trace_rows)

        candidates: list[dict[str, Any]] = []

        def add_candidate(
            category: str,
            title: str,
            score: float,
            support: list[dict[str, Any]],
            contradict: list[dict[str, Any]],
        ) -> None:
            candidates.append(
                {
                    "id": new_id(),
                    "rank": 0,
                    "category": category,
                    "title": title,
                    "confidence": round(min(max(score, 0.0), 0.99), 2),
                    "verdict": "SUPPORTED" if score >= 0.55 else "UNCONFIRMED",
                    "supporting_evidence_ids": [item["id"] for item in support if item],
                    "contradicting_evidence_ids": [item["id"] for item in contradict if item],
                }
            )

        outage_score = (
            (0.45 if health_down else 0)
            + (0.25 if refused else 0)
            + (0.2 if dependency_mentions else 0)
            + (0.1 if error_rate >= 0.05 else 0)
        )
        add_candidate(
            "DEPENDENCY_OUTAGE",
            "Redis/MySQL/RocketMQ dependency is unavailable",
            outage_score,
            [health if health_down else {}, logs if dependency_mentions or refused else {}, red],
            [health if not health_down else {}],
        )

        latency_score = (
            (0.35 if p95 >= 1.0 else 0)
            + (0.3 if timeouts else 0)
            + (0.25 if slow_trace else 0)
            + (0.1 if health_up else 0)
        )
        add_candidate(
            "DEPENDENCY_LATENCY",
            "A downstream dependency is slow or timing out",
            latency_score,
            [red if p95 >= 1.0 else {}, logs if timeouts else {}, traces if slow_trace else {}],
            [red if p95 < 1.0 else {}],
        )

        cpu_score = (
            (0.7 if cpu >= 0.8 else 0)
            + (0.15 if p95 >= 1.0 else 0)
            + (0.1 if error_rate >= 0.05 else 0)
            + (0.05 if logs_available and not dependency_mentions else 0)
        )
        add_candidate(
            "CPU_SATURATION",
            "MerchantFlow process CPU is saturated",
            cpu_score,
            [jvm if cpu >= 0.8 else {}, red if p95 >= 1.0 else {}],
            [logs if dependency_mentions else {}],
        )

        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return {"hypotheses": candidates[:3]}

    async def _build_report(self, state: InvestigationState) -> dict[str, Any]:
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
        conclusive = top["confidence"] >= 0.65 and len(available_sources) >= 2
        root_cause = top["title"] if conclusive else "Evidence is insufficient"
        recommendations = {
            "CPU_SATURATION": [
                "Confirm the hot endpoint and JVM thread activity before scaling",
                "Reduce load or increase the container CPU quota in a controlled rollout",
            ],
            "DEPENDENCY_LATENCY": [
                "Inspect the slow Redis/MySQL span and connection pool saturation",
                "Remove the lab proxy latency only after operator approval",
            ],
            "DEPENDENCY_OUTAGE": [
                "Verify dependency reachability and credentials from the application network",
                "Restore the failed dependency or proxy route in the isolated lab",
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
        report = (
            f"# Incident diagnosis\n\n## Conclusion\n{summary}\n\n"
            f"## Ranked hypotheses\n{hypothesis_lines}\n\n"
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
                "unavailable_sources": unavailable_sources,
                "report_markdown": report,
            }
        }

    def _compile(self, checkpointer: Any) -> Any:
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
