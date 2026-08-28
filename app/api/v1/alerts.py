from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.db import get_session
from app.services.incident_repository import incident_repository

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _authorized_webhook(
    body: bytes,
    authorization: str | None,
    token: str | None,
    signature: str | None,
) -> bool:
    secret = config.alertmanager_webhook_secret
    if not secret:
        return config.environment in {"local", "test"}
    if authorization == f"Bearer {secret}" or token == secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature and hmac.compare_digest(signature, expected))


@router.post("/alertmanager", status_code=status.HTTP_202_ACCEPTED)
async def receive_alertmanager_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_alertmanager_token: str | None = Header(default=None),
    x_aiops_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    if not _authorized_webhook(
        body, authorization, x_alertmanager_token, x_aiops_signature
    ):
        raise HTTPException(status_code=401, detail="invalid Alertmanager webhook credential")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise HTTPException(status_code=400, detail="Alertmanager payload requires alerts[]")

    accepted: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        incident, created = await incident_repository.upsert_alert(session, alert)
        run_id = None
        if alert.get("status", "firing") == "firing" and config.aiops_auto_diagnosis_enabled:
            idempotency_key = (
                f"alert:{incident.fingerprint}:{alert.get('startsAt') or 'active'}"
            )
            run, _queued = await incident_repository.queue_diagnosis(
                session,
                incident,
                trigger="alert",
                requested_by="alertmanager",
                idempotency_key=idempotency_key,
            )
            run_id = run.id
        accepted.append(
            {
                "incident_id": incident.id,
                "created": created,
                "diagnosis_run_id": run_id,
                "status": incident.status.value,
            }
        )
    await session.commit()
    return {"accepted": accepted, "count": len(accepted)}


# One-release compatibility alias used by the existing Alertmanager config.
@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def legacy_alertmanager_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_alertmanager_token: str | None = Header(default=None),
    x_aiops_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        await receive_alertmanager_webhook(
            request, session, authorization, x_alertmanager_token, x_aiops_signature
        ),
    )
