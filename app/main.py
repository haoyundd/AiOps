"""FastAPI control-plane entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from sqlalchemy import text

from app.api.v1 import alerts, auth, chat, incidents, models, remediation, runbooks, services
from app.config import config
from app.db import AsyncSessionFactory, close_database, init_database
from app.services.bootstrap_service import bootstrap_database


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_database()
    await bootstrap_database()
    yield
    await close_database()


app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="Evidence-driven incident investigation for containerized services",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Idempotency-Key"],
)

api_prefix = "/api/v1"
app.include_router(auth.router, prefix=api_prefix)
app.include_router(alerts.router, prefix=api_prefix)
app.include_router(incidents.router, prefix=api_prefix)
app.include_router(incidents.diagnoses_router, prefix=api_prefix)
app.include_router(services.router, prefix=api_prefix)
app.include_router(runbooks.router, prefix=api_prefix)
app.include_router(remediation.router, prefix=api_prefix)
app.include_router(chat.router, prefix=api_prefix)
app.include_router(models.router, prefix=api_prefix)

app.mount("/internal/metrics", make_asgi_app())

static_dir = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "UP", "service": config.app_name, "version": config.app_version}


@app.get("/ready")
async def ready() -> dict[str, str]:
    async with AsyncSessionFactory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "UP", "database": "UP"}


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(static_dir / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.host, port=config.port, reload=config.debug)
