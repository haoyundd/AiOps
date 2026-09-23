from fastapi import APIRouter, Depends, Query, status

from app.schemas import RunbookCreate
from app.security import AuthenticatedUser, admin_required, viewer_required
from app.services.runbook_service import runbook_service

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


@router.get("")
async def search_runbooks(
    service_name: str = Query(default="merchantflow", min_length=1, max_length=120),
    query: str = Query(default="", max_length=500),
    limit: int = Query(default=10, ge=1, le=10),
    _current_user: AuthenticatedUser = Depends(viewer_required),
) -> list[dict[str, object]]:
    """提供只读知识检索入口，前端和后续 Agent 都复用同一检索服务。"""
    return await runbook_service.search(service_name, query or service_name, limit=limit)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_runbook(
    payload: RunbookCreate,
    current_user: AuthenticatedUser = Depends(admin_required),
) -> dict[str, object]:
    document, created = await runbook_service.create(
        title=payload.title,
        service_name=payload.service_name,
        tags=payload.tags,
        content=payload.content,
        created_by=current_user.username,
    )
    return {"id": document.id, "created": created, "checksum": document.checksum}
