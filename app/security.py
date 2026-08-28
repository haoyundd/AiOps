"""Local JWT authentication and role-based authorization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.db import User, get_session
from app.domain import Role

password_hasher = PasswordHasher()
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    username: str
    role: Role


def hash_password(password: str) -> str:
    return str(password_hasher.hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(password_hasher.verify(password_hash, password))
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(user: User) -> tuple[str, int]:
    expires = datetime.now(UTC) + timedelta(minutes=config.jwt_expire_minutes)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role.value,
        "exp": expires,
        "iat": datetime.now(UTC),
    }
    token = jwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm)
    return token, config.jwt_expire_minutes * 60


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> AuthenticatedUser:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = jwt.decode(
            credentials.credentials,
            config.jwt_secret,
            algorithms=[config.jwt_algorithm],
        )
        user_id = str(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise unauthorized from exc

    user = await session.scalar(select(User).where(User.id == user_id, User.active.is_(True)))
    if user is None:
        raise unauthorized
    return AuthenticatedUser(id=user.id, username=user.username, role=user.role)


def require_roles(*allowed_roles: Role) -> Callable[..., Awaitable[AuthenticatedUser]]:
    async def dependency(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="insufficient role")
        return current_user

    return dependency


viewer_required = require_roles(Role.VIEWER, Role.OPERATOR, Role.ADMIN)
operator_required = require_roles(Role.OPERATOR, Role.ADMIN)
admin_required = require_roles(Role.ADMIN)
