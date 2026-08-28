"""Isolated, credential-free test environment."""

import os
import sys
from pathlib import Path

import pytest_asyncio
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite+aiosqlite:///./data/test-aiops.db",
        "DATABASE_AUTO_CREATE": "true",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "test-admin-password",
        "JWT_SECRET": "test-jwt-secret-long-enough-for-tests",
        "ALERTMANAGER_WEBHOOK_SECRET": "test-alert-secret",
        "AIOPS_AUTO_DIAGNOSIS_ENABLED": "false",
        "DASHSCOPE_API_KEY": "",
    }
)

from app.db import Base, engine
from app.main import app
from app.services.bootstrap_service import bootstrap_database


@pytest_asyncio.fixture(autouse=True)
async def reset_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await bootstrap_database()
    yield


@pytest_asyncio.fixture
async def client():
    with TestClient(app) as value:
        yield value


@pytest_asyncio.fixture
async def admin_token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]
