from fastapi import APIRouter, Depends, status

from app.schemas import RunbookCreate
from app.security import AuthenticatedUser, admin_required
from app.services.runbook_service import runbook_service

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


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
