"""Harness Engineering 持久化数据模型。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """生成统一的 UTC 时间，便于跨容器和本地环境排序。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类。"""


class IncidentRecord(Base):
    """告警 incident 主表，保存告警生命周期和最终报告。"""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    alert_status: Mapped[str] = mapped_column(String(32), default="firing")
    service_name: Mapped[str] = mapped_column(String(128), index=True)
    alert_name: Mapped[str] = mapped_column(String(128), index=True)
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    metric_name: Mapped[str] = mapped_column(String(128), default="")
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    environment: Mapped[str] = mapped_column(String(64), default="local")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_alert_json: Mapped[str] = mapped_column(Text, default="{}")
    report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    timeline_events: Mapped[list["TimelineEventRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="TimelineEventRecord.created_at",
    )
    agent_runs: Mapped[list["AgentRunRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="AgentRunRecord.started_at",
    )
    command_actions: Mapped[list["IncidentCommandActionRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentCommandActionRecord.created_at",
    )


class TimelineEventRecord(Base):
    """incident 时间线事件表，记录 Agent 每个阶段的可观测事件。"""

    __tablename__ = "incident_timeline_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    incident: Mapped[IncidentRecord] = relationship(back_populates="timeline_events")


class AgentRunRecord(Base):
    """一次 Agent 诊断运行，和 incident 解耦以支持重复诊断。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="webhook")
    model_provider: Mapped[str] = mapped_column(String(64), default="")
    model_name: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    total_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    incident: Mapped[IncidentRecord] = relationship(back_populates="agent_runs")
    state: Mapped["AgentStateRecord | None"] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        uselist=False,
    )


class AgentStateRecord(Base):
    """Agent 最新状态快照，第一阶段用于回放和审计，不做断点续跑。"""

    __tablename__ = "agent_states"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    agent_run: Mapped[AgentRunRecord] = relationship(back_populates="state")


class IncidentCommandActionRecord(Base):
    """incident 指挥动作表，记录人工处置和事件指挥过程。"""

    __tablename__ = "incident_command_actions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="operator")
    note: Mapped[str] = mapped_column(Text, default="")
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    incident: Mapped[IncidentRecord] = relationship(back_populates="command_actions")
