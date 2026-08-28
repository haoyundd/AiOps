from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, get_session
from app.schemas import LoginRequest, TokenResponse, UserRead
from app.security import (
    AuthenticatedUser,
    create_access_token,
    get_current_user,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")
    token, expires_in = create_access_token(user)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        role=user.role,
        username=user.username,
    )


@router.get("/me", response_model=UserRead)
async def me(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> UserRead:
    return UserRead(
        id=current_user.id,
        username=current_user.username,
        role=current_user.role,
        active=True,
    )
