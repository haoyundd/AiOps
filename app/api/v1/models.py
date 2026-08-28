from fastapi import APIRouter, Depends, HTTPException

from app.core.llm_factory import llm_factory
from app.schemas import ModelSwitchRequest
from app.security import AuthenticatedUser, admin_required, viewer_required

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/current")
async def current_model(
    _current_user: AuthenticatedUser = Depends(viewer_required),
) -> dict[str, str]:
    return llm_factory.get_public_config()


@router.get("/options")
async def model_options(
    _current_user: AuthenticatedUser = Depends(viewer_required),
) -> dict[str, dict[str, str]]:
    return llm_factory.list_options()


@router.post("/switch")
async def switch_model(
    payload: ModelSwitchRequest,
    _current_user: AuthenticatedUser = Depends(admin_required),
) -> dict[str, str]:
    try:
        return llm_factory.switch_provider(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
