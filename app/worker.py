"""Durable PostgreSQL-backed diagnosis worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.agent.evidence_graph import evidence_diagnosis_graph
from app.config import config
from app.core.llm_factory import llm_factory
from app.db import (
    DiagnosisRun,
    Incident,
    MonitoredService,
    RemediationProposal,
    init_database,
    session_scope,
)
from app.domain import DiagnosisStatus, IncidentStatus, RiskLevel, ensure_incident_transition
from app.services.bootstrap_service import bootstrap_database
from app.services.incident_repository import incident_repository
from app.services.runbook_draft_service import runbook_draft_service


def _incident_payload(incident: Any) -> dict[str, Any]:
    """把 Incident ORM 对象转换成诊断图使用的普通字典。"""
    return {
        "id": incident.id,
        "service_name": incident.service_name,
        "environment": incident.environment,
        "alert_name": incident.alert_name,
        "severity": incident.severity,
        "metric_name": incident.metric_name,
        "threshold": incident.threshold,
        "description": incident.description,
        "raw_alert": incident.raw_alert,
        "created_at": incident.created_at.isoformat(),
        "updated_at": incident.updated_at.isoformat(),
    }


def _service_payload(service: MonitoredService | None, incident: Any) -> dict[str, Any]:
    """把被监控服务配置转换成观测工具需要的标签和健康地址。"""
    if service is None:
        return {
            "name": incident.service_name,
            "environment": incident.environment,
            "prometheus_labels": {"job": incident.service_name},
            "loki_labels": {"service_name": incident.service_name},
            "tempo_service_name": incident.service_name,
            "health_url": config.merchantflow_health_url,
            "allow_mutations": False,
        }
    return {
        "id": service.id,
        "name": service.name,
        "environment": service.environment,
        "prometheus_labels": service.prometheus_labels,
        "loki_labels": service.loki_labels,
        "tempo_service_name": service.tempo_service_name,
        "health_url": service.health_url,
        "allow_mutations": service.allow_mutations,
    }


class DiagnosisWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run_once(self) -> bool:
        async with session_scope() as session:
            claimed = await incident_repository.claim_next_run(session, self.worker_id)
            if claimed is None:
                return False
            run, incident = claimed
            # 领取任务时保存真实运行配置，详情页不会再展示测试或历史配置。
            runtime = llm_factory.get_runtime_config()
            run.provider = runtime.provider or None
            run.model = runtime.model or None
            await session.flush()
            service = await session.scalar(
                select(MonitoredService).where(
                    MonitoredService.name == incident.service_name,
                    MonitoredService.environment == incident.environment,
                )
            )
            run_id = run.id
            incident_payload = _incident_payload(incident)
            service_payload = _service_payload(service, incident)

        try:
            result = await evidence_diagnosis_graph.run(
                run_id=run_id,
                incident=incident_payload,
                service=service_payload,
            )
            conclusion = result["conclusion"]
            status = DiagnosisStatus(conclusion["status"])
            async with session_scope() as session:
                run = await session.get(DiagnosisRun, run_id)
                incident = await session.get(Incident, incident_payload["id"])
                if run is None or incident is None:
                    raise RuntimeError("claimed diagnosis disappeared before completion")
                await incident_repository.finish_diagnosis(
                    session,
                    run,
                    incident,
                    status=status,
                    conclusion=conclusion,
                    evidence=result["evidence"],
                    hypotheses=result["hypotheses"],
                    tool_calls=result["tool_calls"],
                    # 步骤数来自图运行时实际返回的节点轨迹，不再把当前节点数写死为 4。
                    # 下一步：前端可利用 executed_steps 展示每个节点的执行过程。
                    total_steps=len(result.get("executed_steps", [])) or None,
                    total_tool_calls=len(result["tool_calls"]),
                )
                draft = await runbook_draft_service.create_from_diagnosis(
                    session,
                    incident,
                    run,
                    conclusion,
                    result["evidence"],
                )
                if draft is not None:
                    await incident_repository.append_event(
                        session,
                        incident.id,
                        "runbook_draft_created",
                        "A diagnosed incident generated a runbook draft for review",
                        payload={"draft_id": draft.id, "checksum": draft.checksum},
                    )
                # 下一步：只有 LAB_MODE 与 ALLOW_MUTATIONS 同时开启时，才会把这些建议送入审批执行流。
                action_by_category = {
                    "REDIS_LATENCY": "remove_redis_latency",
                    "REDIS_OUTAGE": "restore_redis_connection",
                }
                action_id = action_by_category.get(str(conclusion.get("category", "")))
                if action_id and service_payload.get("allow_mutations"):
                    key = hashlib.sha256(f"{run.id}:{action_id}".encode()).hexdigest()
                    proposal = RemediationProposal(
                        incident_id=incident.id,
                        diagnosis_run_id=run.id,
                        action_id=action_id,
                        parameters={},
                        reason=f"Lab-only recovery proposed for {conclusion.get('root_cause')}",
                        risk_level=RiskLevel.LOW,
                        idempotency_key=key,
                    )
                    session.add(proposal)
                    ensure_incident_transition(incident.status, IncidentStatus.WAITING_APPROVAL)
                    incident.status = IncidentStatus.WAITING_APPROVAL
                    await session.flush()
                    await incident_repository.append_event(
                        session,
                        incident.id,
                        "remediation_proposed",
                        f"Lab action {action_id} is waiting for operator approval",
                        payload={"proposal_id": proposal.id, "risk_level": "LOW"},
                    )
            logger.info("diagnosis {} completed with {}", run_id, status.value)
        except Exception as exc:
            logger.exception("diagnosis {} failed", run_id)
            async with session_scope() as session:
                run = await session.get(DiagnosisRun, run_id)
                incident = await session.get(Incident, incident_payload["id"])
                if run is not None and incident is not None:
                    await incident_repository.finish_diagnosis(
                        session,
                        run,
                        incident,
                        status=DiagnosisStatus.FAILED,
                        conclusion={"summary": "Diagnosis failed", "error": str(exc)},
                        evidence=[],
                        hypotheses=[],
                        tool_calls=[],
                        total_steps=None,
                        total_tool_calls=None,
                        error=str(exc),
                    )
        return True

    async def run_forever(self) -> None:
        await init_database()
        await bootstrap_database()
        logger.info("diagnosis worker {} started", self.worker_id)
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(config.diagnosis_poll_interval_seconds)


async def main() -> None:
    await DiagnosisWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
