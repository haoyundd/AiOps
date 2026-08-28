"""Versioned API and tool contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain import DiagnosisStatus, IncidentStatus, ProposalStatus, RiskLevel, Role


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: Role
    username: str


class UserRead(BaseModel):
    id: str
    username: str
    role: Role
    active: bool


class ServiceRead(BaseModel):
    id: str
    name: str
    environment: str
    description: str
    health_url: str
    dependencies: list[dict[str, Any]]
    allow_mutations: bool
    active: bool


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="local", max_length=40)
    description: str = ""
    prometheus_labels: dict[str, Any] = Field(default_factory=dict)
    loki_labels: dict[str, Any] = Field(default_factory=dict)
    tempo_service_name: str = ""
    health_url: str = ""
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    runbook_tags: list[str] = Field(default_factory=list)
    allow_mutations: bool = False


class IncidentEventRead(BaseModel):
    id: int
    event_type: str
    message: str
    payload: dict[str, Any]
    actor: str
    created_at: datetime


class DiagnosisRunRead(BaseModel):
    id: str
    status: DiagnosisStatus
    trigger: str
    requested_by: str
    conclusion: dict[str, Any]
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class IncidentRead(BaseModel):
    id: str
    fingerprint: str
    service_name: str
    environment: str
    alert_name: str
    alert_status: str
    severity: str
    metric_name: str
    threshold: float | None
    description: str
    status: IncidentStatus
    version: int
    raw_alert: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    events: list[IncidentEventRead] = Field(default_factory=list)
    diagnoses: list[DiagnosisRunRead] = Field(default_factory=list)


class IncidentListResponse(BaseModel):
    items: list[IncidentRead]
    total: int
    limit: int
    offset: int


class DiagnosisCreate(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class ToolPolicy(BaseModel):
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    read_only: bool = True
    timeout_seconds: float = 8.0
    retries: int = 1
    max_time_range_minutes: int = 120
    max_result_count: int = 200


class ToolResult(BaseModel):
    tool_call_id: str
    source: str
    status: Literal["success", "unavailable", "error", "rejected"]
    query: str = ""
    time_range: dict[str, str] = Field(default_factory=dict)
    observed_at: datetime
    data: Any = Field(default_factory=dict)
    summary: str = ""
    error: str = ""
    truncated: bool = False


class EvidenceRead(BaseModel):
    id: str
    source: str
    kind: str
    status: str
    summary: str
    query: str
    data: dict[str, Any]
    observed_at: datetime


class HypothesisRead(BaseModel):
    id: str
    rank: int
    category: str
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    verdict: str
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]


class DiagnosisDetail(DiagnosisRunRead):
    evidence: list[EvidenceRead] = Field(default_factory=list)
    hypotheses: list[HypothesisRead] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class RunbookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    service_name: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list)
    content: str = Field(min_length=20)


class RemediationDecision(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class RemediationProposalRead(BaseModel):
    id: str
    incident_id: str
    action_id: str
    parameters: dict[str, Any]
    reason: str
    risk_level: RiskLevel
    status: ProposalStatus
    proposed_by: str
    decided_by: str
    result: dict[str, Any]
    created_at: datetime


class ModelSwitchRequest(BaseModel):
    provider: Literal["dashscope", "xiaomi", "custom"]
    model: str | None = Field(default=None, max_length=160)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
