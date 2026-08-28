"""Allowlisted sandbox recovery executor with post-action health verification."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx

from app.config import config
from app.db import AuditEvent, Incident, RemediationProposal, session_scope
from app.domain import IncidentStatus, ProposalStatus, ensure_incident_transition
from app.observability.client import observability_client
from app.services.incident_repository import incident_repository


class RemediationService:
    allowed_actions = {"remove_redis_latency", "restore_redis_connection"}

    async def _apply_action(self, action_id: str) -> dict[str, Any]:
        if action_id not in self.allowed_actions:
            raise ValueError("action is not in the sandbox allowlist")
        base = config.toxiproxy_url.rstrip("/")
        async with httpx.AsyncClient(timeout=8.0) as client:
            if action_id == "remove_redis_latency":
                response = await client.delete(
                    f"{base}/proxies/redis/toxics/latency_downstream"
                )
                if response.status_code not in {204, 404}:
                    response.raise_for_status()
                return {"action": action_id, "toxic_removed": response.status_code == 204}
            response = await client.post(
                f"{base}/proxies/redis",
                json={
                    "name": "redis",
                    "listen": "0.0.0.0:16379",
                    "upstream": "redis:6379",
                    "enabled": True,
                },
            )
            response.raise_for_status()
            return {"action": action_id, "proxy_enabled": True}

    async def _verify_health(self) -> dict[str, Any]:
        last_result: dict[str, Any] = {}
        for _attempt in range(5):
            result = await observability_client.get_service_health(
                config.merchantflow_health_url
            )
            last_result = result.model_dump(mode="json")
            if result.status == "success" and result.data.get("state") == "UP":
                return last_result
            await asyncio.sleep(2)
        return last_result

    async def execute(self, proposal_id: str, actor: str) -> RemediationProposal:
        if not config.lab_mode or not config.allow_mutations:
            raise PermissionError("sandbox mutations are disabled")
        async with session_scope() as session:
            proposal = await session.get(RemediationProposal, proposal_id, with_for_update=True)
            if proposal is None:
                raise LookupError("remediation proposal not found")
            if proposal.status != ProposalStatus.APPROVED:
                raise ValueError("proposal must be approved before execution")
            incident = await session.get(Incident, proposal.incident_id, with_for_update=True)
            if incident is None:
                raise LookupError("incident not found")
            ensure_incident_transition(incident.status, IncidentStatus.REMEDIATING)
            incident.status = IncidentStatus.REMEDIATING
            proposal.status = ProposalStatus.EXECUTING
            await incident_repository.append_event(
                session,
                incident.id,
                "remediation_started",
                f"Executing allowlisted lab action {proposal.action_id}",
                payload={"proposal_id": proposal.id, "action_id": proposal.action_id},
                actor=actor,
            )

        try:
            action_result = await self._apply_action(proposal.action_id)
            async with session_scope() as session:
                proposal = await session.get(RemediationProposal, proposal_id, with_for_update=True)
                incident = await session.get(Incident, proposal.incident_id, with_for_update=True)
                ensure_incident_transition(incident.status, IncidentStatus.VERIFYING)
                incident.status = IncidentStatus.VERIFYING
                await incident_repository.append_event(
                    session,
                    incident.id,
                    "remediation_verifying",
                    "Recovery action completed; verifying real service health",
                    payload=action_result,
                    actor=actor,
                )

            health = await self._verify_health()
            healthy = health.get("status") == "success" and health.get("data", {}).get("state") == "UP"
            async with session_scope() as session:
                proposal = await session.get(RemediationProposal, proposal_id, with_for_update=True)
                incident = await session.get(Incident, proposal.incident_id, with_for_update=True)
                target = IncidentStatus.RESOLVED if healthy else IncidentStatus.FAILED
                ensure_incident_transition(incident.status, target)
                incident.status = target
                proposal.status = ProposalStatus.VERIFIED if healthy else ProposalStatus.FAILED
                proposal.result = {"action": action_result, "health_verification": health}
                session.add(
                    AuditEvent(
                        actor=actor,
                        action="remediation.execute",
                        resource_type="remediation_proposal",
                        resource_id=proposal.id,
                        outcome="verified" if healthy else "failed",
                        detail=proposal.result,
                    )
                )
                await incident_repository.append_event(
                    session,
                    incident.id,
                    "remediation_verified" if healthy else "remediation_verification_failed",
                    "Service health recovered" if healthy else "Service health did not recover",
                    payload={"proposal_id": proposal.id, "health": health},
                    actor=actor,
                )
            return cast(RemediationProposal, proposal)
        except Exception as exc:
            async with session_scope() as session:
                proposal = await session.get(RemediationProposal, proposal_id, with_for_update=True)
                if proposal is None:
                    raise
                incident = await session.get(Incident, proposal.incident_id, with_for_update=True)
                proposal.status = ProposalStatus.FAILED
                proposal.result = {"error": str(exc)}
                if incident is not None:
                    incident.status = IncidentStatus.FAILED
                    await incident_repository.append_event(
                        session,
                        incident.id,
                        "remediation_failed",
                        str(exc),
                        payload={"proposal_id": proposal.id},
                        actor=actor,
                    )
            raise


remediation_service = RemediationService()
