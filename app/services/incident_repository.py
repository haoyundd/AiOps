"""Durable incident, diagnosis, evidence, and event persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import (
    DiagnosisRun,
    EvidenceItem,
    HypothesisRecord,
    Incident,
    IncidentEvent,
    ToolCallRecord,
    utcnow,
)
from app.domain import DiagnosisStatus, IncidentStatus, RiskLevel, ensure_incident_transition


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_alert_fingerprint(alert: dict[str, Any]) -> str:
    supplied = alert.get("fingerprint")
    # Alertmanager 的 fingerprint 通常标识规则和标签，不一定区分恢复后的下一轮 firing。
    # 下一步：把 startsAt 纳入内部去重键，同一轮仍幂等，不同轮次创建新的 Incident。
    if supplied and not alert.get("startsAt"):
        return str(supplied)
    labels = alert.get("labels") or {}
    stable = {
        "supplied_fingerprint": supplied,
        "alertname": labels.get("alertname"),
        "service": labels.get("service") or labels.get("job"),
        "environment": labels.get("environment", "local"),
        "startsAt": alert.get("startsAt"),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


class IncidentRepository:
    async def upsert_alert(
        self,
        session: AsyncSession,
        alert: dict[str, Any],
        *,
        actor: str = "alertmanager",
    ) -> tuple[Incident, bool]:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        fingerprint = build_alert_fingerprint(alert)
        incident = await session.scalar(
            select(Incident).where(Incident.fingerprint == fingerprint).with_for_update()
        )
        created = incident is None
        if incident is None:
            incident = Incident(
                fingerprint=fingerprint,
                service_name=str(labels.get("service") or labels.get("job") or "unknown"),
                environment=str(labels.get("environment") or "local"),
                alert_name=str(labels.get("alertname") or "UnknownAlert"),
                alert_status=str(alert.get("status") or "firing"),
                severity=str(labels.get("severity") or "warning"),
                metric_name=str(labels.get("metric") or ""),
                threshold=_float_or_none(labels.get("threshold") or annotations.get("threshold")),
                description=str(annotations.get("description") or annotations.get("summary") or ""),
                status=IncidentStatus.RECEIVED,
                raw_alert=alert,
                started_at=_parse_datetime(alert.get("startsAt")),
            )
            session.add(incident)
            await session.flush()
            await self.append_event(
                session,
                incident.id,
                "incident_received",
                "Alertmanager created the incident",
                payload={"fingerprint": fingerprint},
                actor=actor,
            )
        else:
            incident.alert_status = str(alert.get("status") or incident.alert_status)
            incident.severity = str(labels.get("severity") or incident.severity)
            incident.description = str(
                annotations.get("description") or annotations.get("summary") or incident.description
            )
            incident.raw_alert = alert
            incident.version += 1
            await self.append_event(
                session,
                incident.id,
                "incident_updated",
                "Alertmanager updated the incident",
                payload={"alert_status": incident.alert_status},
                actor=actor,
            )

        if alert.get("status") == "resolved" and incident.status != IncidentStatus.RESOLVED:
            ensure_incident_transition(incident.status, IncidentStatus.RESOLVED)
            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = utcnow()
            await self.append_event(
                session,
                incident.id,
                "alert_resolved",
                "Alertmanager reported the alert as resolved",
                actor=actor,
            )
        return incident, created

    async def append_event(
        self,
        session: AsyncSession,
        incident_id: str,
        event_type: str,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> IncidentEvent:
        event = IncidentEvent(
            incident_id=incident_id,
            event_type=event_type,
            message=message,
            payload=payload or {},
            actor=actor,
        )
        session.add(event)
        await session.flush()
        return event

    async def queue_diagnosis(
        self,
        session: AsyncSession,
        incident: Incident,
        *,
        trigger: str,
        requested_by: str,
        idempotency_key: str,
    ) -> tuple[DiagnosisRun, bool]:
        existing = await session.scalar(
            select(DiagnosisRun).where(
                DiagnosisRun.incident_id == incident.id,
                DiagnosisRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing, False
        run = DiagnosisRun(
            incident_id=incident.id,
            trigger=trigger,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        session.add(run)
        await session.flush()
        await self.append_event(
            session,
            incident.id,
            "diagnosis_queued",
            "Diagnosis was queued for the worker",
            payload={"run_id": run.id, "trigger": trigger},
            actor=requested_by,
        )
        return run, True

    async def get_incident(
        self, session: AsyncSession, incident_id: str, *, with_details: bool = True
    ) -> Incident | None:
        query = select(Incident).where(Incident.id == incident_id)
        if with_details:
            query = query.options(
                selectinload(Incident.events), selectinload(Incident.diagnoses)
            )
        return cast(Incident | None, await session.scalar(query))

    async def list_incidents(
        self,
        session: AsyncSession,
        *,
        status: IncidentStatus | None = None,
        service_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Incident], int]:
        filters: list[Any] = []
        if status is not None:
            filters.append(Incident.status == status)
        if service_name:
            filters.append(Incident.service_name == service_name)
        count = await session.scalar(select(func.count(Incident.id)).where(*filters))
        query = (
            select(Incident)
            .where(*filters)
            .options(selectinload(Incident.events), selectinload(Incident.diagnoses))
            .order_by(Incident.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        incidents = list((await session.scalars(query)).unique().all())
        return incidents, int(count or 0)

    async def claim_next_run(
        self, session: AsyncSession, worker_id: str
    ) -> tuple[DiagnosisRun, Incident] | None:
        query = (
            select(DiagnosisRun)
            .where(DiagnosisRun.status == DiagnosisStatus.QUEUED)
            .order_by(DiagnosisRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        run = await session.scalar(query)
        if run is None:
            return None
        incident = await session.get(Incident, run.incident_id, with_for_update=True)
        if incident is None:
            run.status = DiagnosisStatus.FAILED
            run.error = "incident no longer exists"
            run.finished_at = utcnow()
            return None

        ensure_incident_transition(incident.status, IncidentStatus.DIAGNOSING)
        incident.status = IncidentStatus.DIAGNOSING
        incident.version += 1
        run.status = DiagnosisStatus.RUNNING
        run.worker_id = worker_id
        run.started_at = utcnow()
        await self.append_event(
            session,
            incident.id,
            "diagnosis_started",
            "Evidence-driven investigation started",
            payload={"run_id": run.id, "worker_id": worker_id},
        )
        await session.flush()
        return run, incident

    async def finish_diagnosis(
        self,
        session: AsyncSession,
        run: DiagnosisRun,
        incident: Incident,
        *,
        status: DiagnosisStatus,
        conclusion: dict[str, Any],
        evidence: list[dict[str, Any]],
        hypotheses: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        total_steps: int | None = None,
        total_tool_calls: int | None = None,
        error: str = "",
    ) -> None:
        run.status = status
        run.conclusion = conclusion
        run.error = error
        run.total_steps = total_steps
        run.total_tool_calls = total_tool_calls
        run.finished_at = utcnow()

        evidence_records: list[EvidenceItem] = []
        for item in evidence:
            record = EvidenceItem(diagnosis_run_id=run.id, **item)
            session.add(record)
            evidence_records.append(record)
        await session.flush()

        for item in hypotheses:
            session.add(HypothesisRecord(diagnosis_run_id=run.id, **item))
        for item in tool_calls:
            item.setdefault("risk_level", RiskLevel.READ_ONLY)
            session.add(ToolCallRecord(diagnosis_run_id=run.id, **item))

        target = {
            DiagnosisStatus.DIAGNOSED: IncidentStatus.DIAGNOSED,
            DiagnosisStatus.INCONCLUSIVE: IncidentStatus.INCONCLUSIVE,
            DiagnosisStatus.FAILED: IncidentStatus.FAILED,
        }[status]
        ensure_incident_transition(incident.status, target)
        incident.status = target
        incident.version += 1
        await self.append_event(
            session,
            incident.id,
            "diagnosis_completed" if status != DiagnosisStatus.FAILED else "diagnosis_failed",
            conclusion.get("summary") or error or "Diagnosis finished",
            payload={
                "run_id": run.id,
                "status": status.value,
                "root_cause": conclusion.get("root_cause"),
                "confidence": conclusion.get("confidence"),
            },
        )


def incident_to_dict(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "service_name": incident.service_name,
        "environment": incident.environment,
        "alert_name": incident.alert_name,
        "alert_status": incident.alert_status,
        "severity": incident.severity,
        "metric_name": incident.metric_name,
        "threshold": incident.threshold,
        "description": incident.description,
        "status": incident.status,
        "version": incident.version,
        "raw_alert": incident.raw_alert,
        "started_at": incident.started_at,
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
        "resolved_at": incident.resolved_at,
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "message": event.message,
                "payload": event.payload,
                "actor": event.actor,
                "created_at": event.created_at,
            }
            for event in getattr(incident, "events", [])
        ],
        "diagnoses": [diagnosis_to_dict(run) for run in getattr(incident, "diagnoses", [])],
    }


def diagnosis_to_dict(run: DiagnosisRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "requested_by": run.requested_by,
        "provider": run.provider,
        "model": run.model,
        "total_steps": run.total_steps,
        "total_tool_calls": run.total_tool_calls,
        "conclusion": run.conclusion,
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


incident_repository = IncidentRepository()
