"""Async database engine, ORM models, and bootstrap helpers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import config
from app.domain import (
    DiagnosisStatus,
    IncidentStatus,
    ProposalStatus,
    RiskLevel,
    Role,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def enum_column(enum_type: type[Any]) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(enum_column(Role), default=Role.VIEWER)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MonitoredService(Base):
    __tablename__ = "monitored_services"
    __table_args__ = (UniqueConstraint("name", "environment", name="uq_service_environment"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), index=True)
    environment: Mapped[str] = mapped_column(String(40), default="local")
    description: Mapped[str] = mapped_column(Text, default="")
    prometheus_labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    loki_labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tempo_service_name: Mapped[str] = mapped_column(String(120), default="")
    health_url: Mapped[str] = mapped_column(String(500), default="")
    dependencies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    runbook_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    allow_mutations: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fingerprint: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    service_name: Mapped[str] = mapped_column(String(120), index=True)
    environment: Mapped[str] = mapped_column(String(40), default="local", index=True)
    alert_name: Mapped[str] = mapped_column(String(160), index=True)
    alert_status: Mapped[str] = mapped_column(String(30), default="firing")
    severity: Mapped[str] = mapped_column(String(30), default="warning")
    metric_name: Mapped[str] = mapped_column(String(160), default="")
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[IncidentStatus] = mapped_column(
        enum_column(IncidentStatus), default=IncidentStatus.RECEIVED, index=True
    )
    raw_alert: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )

    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.id"
    )
    diagnoses: Mapped[list[DiagnosisRun]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    __table_args__ = (Index("ix_incident_events_incident_id_id", "incident_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="events")


class DiagnosisRun(Base):
    __tablename__ = "diagnosis_runs"
    __table_args__ = (
        UniqueConstraint("incident_id", "idempotency_key", name="uq_diagnosis_idempotency"),
        Index("ix_diagnosis_queue", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[DiagnosisStatus] = mapped_column(
        enum_column(DiagnosisStatus), default=DiagnosisStatus.QUEUED, index=True
    )
    trigger: Mapped[str] = mapped_column(String(30), default="alert")
    idempotency_key: Mapped[str] = mapped_column(String(160))
    requested_by: Mapped[str] = mapped_column(String(120), default="system")
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    conclusion: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="diagnoses")
    evidence: Mapped[list[EvidenceItem]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )
    hypotheses: Mapped[list[HypothesisRecord]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )
    tool_calls: Mapped[list[ToolCallRecord]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )


class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    diagnosis_run_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_runs.id", ondelete="CASCADE"), index=True
    )
    hypothesis_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(30), default="NEUTRAL")
    summary: Mapped[str] = mapped_column(Text)
    query: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    diagnosis: Mapped[DiagnosisRun] = relationship(back_populates="evidence")


class HypothesisRecord(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    diagnosis_run_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_runs.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verdict: Mapped[str] = mapped_column(String(30), default="PENDING")
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    diagnosis: Mapped[DiagnosisRun] = relationship(back_populates="hypotheses")


class ToolCallRecord(Base):
    __tablename__ = "tool_call_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    diagnosis_run_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str] = mapped_column(String(40))
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel), default=RiskLevel.READ_ONLY
    )
    status: Mapped[str] = mapped_column(String(30))
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    diagnosis: Mapped[DiagnosisRun] = relationship(back_populates="tool_calls")


class RunbookDocument(Base):
    __tablename__ = "runbook_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), index=True)
    service_name: Mapped[str] = mapped_column(String(120), index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunbookChunk(Base):
    __tablename__ = "runbook_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("runbook_documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    embedding: Mapped[Any | None] = mapped_column(
        Vector(config.embedding_dimensions).with_variant(JSON(), "sqlite"), nullable=True
    )


class RemediationProposal(Base):
    __tablename__ = "remediation_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    action_id: Mapped[str] = mapped_column(String(120))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[RiskLevel] = mapped_column(enum_column(RiskLevel))
    status: Mapped[ProposalStatus] = mapped_column(
        enum_column(ProposalStatus), default=ProposalStatus.PENDING
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    proposed_by: Mapped[str] = mapped_column(String(120), default="agent")
    decided_by: Mapped[str] = mapped_column(String(120), default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(120), index=True)
    outcome: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _prepare_sqlite_path() -> None:
    prefix = "sqlite+aiosqlite:///"
    if config.database_url.startswith(prefix):
        value = config.database_url.removeprefix(prefix)
        if value and value != ":memory:":
            Path(value).parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_path()
engine: AsyncEngine = create_async_engine(
    config.database_url,
    echo=config.debug,
    pool_pre_ping=True,
)

if config.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_database() -> None:
    async with engine.begin() as connection:
        if config.is_postgres:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if config.database_auto_create:
            await connection.run_sync(Base.metadata.create_all)


async def close_database() -> None:
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
