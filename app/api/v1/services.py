from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import MonitoredService, get_session
from app.schemas import ServiceCreate, ServiceRead
from app.security import AuthenticatedUser, admin_required, viewer_required

router = APIRouter(prefix="/services", tags=["services"])


def _read(service: MonitoredService) -> ServiceRead:
    return ServiceRead(
        id=service.id,
        name=service.name,
        environment=service.environment,
        description=service.description,
        health_url=service.health_url,
        dependencies=service.dependencies,
        allow_mutations=service.allow_mutations,
        active=service.active,
    )


@router.get("", response_model=list[ServiceRead])
async def list_services(
    _current_user: AuthenticatedUser = Depends(viewer_required),
    session: AsyncSession = Depends(get_session),
) -> list[ServiceRead]:
    services = (await session.scalars(select(MonitoredService).order_by(MonitoredService.name))).all()
    return [_read(service) for service in services]


@router.post("", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate,
    _current_user: AuthenticatedUser = Depends(admin_required),
    session: AsyncSession = Depends(get_session),
) -> ServiceRead:
    existing = await session.scalar(
        select(MonitoredService).where(
            MonitoredService.name == payload.name,
            MonitoredService.environment == payload.environment,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="service already exists")
    service = MonitoredService(**payload.model_dump())
    session.add(service)
    await session.commit()
    await session.refresh(service)
    return _read(service)
