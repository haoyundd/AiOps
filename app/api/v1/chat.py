from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm_factory import llm_factory
from app.db import get_session
from app.security import AuthenticatedUser, viewer_required
from app.services.incident_repository import incident_repository

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    incident_id: str | None = None


@router.post("")
async def chat(
    payload: ChatRequest,
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    runtime = llm_factory.get_runtime_config()
    if not runtime.api_key:
        raise HTTPException(status_code=503, detail="LLM API key is not configured")
    context = ""
    if payload.incident_id:
        incident = await incident_repository.get_incident(session, payload.incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        latest = sorted(incident.diagnoses, key=lambda item: item.created_at, reverse=True)
        conclusion = latest[0].conclusion if latest else {}
        context = (
            f"Incident service={incident.service_name}, alert={incident.alert_name}, "
            f"status={incident.status.value}, conclusion={conclusion}. "
        )
    model = llm_factory.create_chat_model(streaming=False)
    response = await model.ainvoke(
        "You are a read-only incident assistant. Base answers only on the supplied incident "
        f"context and state uncertainty explicitly. {context}\nUser: {payload.message}"
    )
    return {"message": str(response.content), "incident_id": payload.incident_id}
