"""Incident 持久化 Repository。"""

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.config import config
from app.db.models import (
    AgentRunRecord,
    AgentStateRecord,
    IncidentCommandActionRecord,
    IncidentRecord,
    TimelineEventRecord,
    utc_now,
)
from app.db.session import init_db, session_scope

MAX_PAYLOAD_CHARS = 4000


def _json_dumps(value: Any, max_chars: int | None = None) -> str:
    """把复杂对象转成 JSON 字符串，并限制超长 payload。"""
    text = json.dumps(value or {}, ensure_ascii=False, default=str)
    if max_chars and len(text) > max_chars:
        return json.dumps(
            {
                "truncated": True,
                "preview": text[:max_chars],
                "original_chars": len(text),
            },
            ensure_ascii=False,
        )
    return text


def _json_loads(value: str | None, default: Any) -> Any:
    """安全解析 JSON 字段，历史脏数据解析失败时返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _iso(value: datetime | None) -> str:
    """统一把数据库时间转成 API 已使用的 ISO 字符串。"""
    return value.isoformat() if value else ""


class IncidentRepository:
    """封装 incident、timeline、agent run 和 state 的持久化读写。"""

    def __init__(self, database_url: str | None = None) -> None:
        """初始化 Repository，并确保数据库表存在。"""
        self.database_url = database_url or config.database_url
        init_db(self.database_url)

    def list_incidents(self) -> List[Dict[str, Any]]:
        """按创建时间倒序返回 incident，并聚合 timeline。"""
        with session_scope(self.database_url) as session:
            records = session.scalars(
                select(IncidentRecord).order_by(IncidentRecord.created_at.desc())
            ).all()
            return [self._incident_to_dict(record) for record in records]

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """查询单个 incident，并聚合 timeline。"""
        with session_scope(self.database_url) as session:
            record = session.get(IncidentRecord, incident_id)
            return self._incident_to_dict(record) if record else None

    def upsert_incident(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        """创建或更新 incident 主记录，保留既有字段语义。"""
        now = utc_now()
        with session_scope(self.database_url) as session:
            record = session.get(IncidentRecord, incident["id"])
            if record is None:
                record = IncidentRecord(
                    id=incident["id"],
                    fingerprint=incident.get("fingerprint", incident["id"]),
                    created_at=self._parse_dt(incident.get("created_at")) or now,
                )
                session.add(record)

            record.status = incident.get("status", record.status)
            record.alert_status = incident.get("alert_status", record.alert_status)
            record.service_name = incident.get("service_name", record.service_name)
            record.alert_name = incident.get("alert_name", record.alert_name)
            record.severity = incident.get("severity", record.severity)
            record.metric_name = incident.get("metric_name", record.metric_name)
            record.threshold = incident.get("threshold")
            record.environment = incident.get("environment", record.environment)
            record.description = incident.get("description")
            record.starts_at = incident.get("starts_at")
            record.raw_alert_json = _json_dumps(incident.get("raw_alert", {}))
            record.report = incident.get("report", record.report or "")
            record.updated_at = now
            session.flush()
            return self._incident_to_dict(record)

    def append_timeline(
        self,
        incident_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        run_id: str | None = None,
    ) -> None:
        """追加 timeline 事件，并对 payload 做长度保护。"""
        with session_scope(self.database_url) as session:
            event = TimelineEventRecord(
                id=uuid.uuid4().hex,
                incident_id=incident_id,
                run_id=run_id,
                event_type=event_type,
                stage=(payload or {}).get("stage") if isinstance(payload, dict) else None,
                message=message,
                payload_json=_json_dumps(payload or {}, MAX_PAYLOAD_CHARS),
                created_at=utc_now(),
            )
            session.add(event)
            record = session.get(IncidentRecord, incident_id)
            if record:
                record.updated_at = utc_now()

    def create_agent_run(
        self,
        incident_id: str,
        trigger_type: str,
        model_provider: str,
        model_name: str,
    ) -> Dict[str, Any]:
        """创建一次新的 Agent Run，保证重复诊断互不覆盖。"""
        run_id = uuid.uuid4().hex
        with session_scope(self.database_url) as session:
            record = AgentRunRecord(
                id=run_id,
                incident_id=incident_id,
                status="running",
                trigger_type=trigger_type,
                model_provider=model_provider,
                model_name=model_name,
                started_at=utc_now(),
            )
            session.add(record)
            session.flush()
            return self._run_to_dict(record)

    def finish_agent_run(
        self,
        run_id: str,
        status: str,
        final_report: str = "",
        error_message: str | None = None,
    ) -> None:
        """结束 Agent Run，并写入最终报告或错误信息。"""
        with session_scope(self.database_url) as session:
            record = session.get(AgentRunRecord, run_id)
            if not record:
                return
            ended_at = utc_now()
            started_at = record.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=ended_at.tzinfo)
            record.status = status
            record.ended_at = ended_at
            record.duration_ms = int((ended_at - started_at).total_seconds() * 1000)
            record.final_report = final_report
            record.error_message = error_message

    def list_runs_for_incident(self, incident_id: str) -> List[Dict[str, Any]]:
        """查询某个 incident 的全部运行记录。"""
        with session_scope(self.database_url) as session:
            records = session.scalars(
                select(AgentRunRecord)
                .where(AgentRunRecord.incident_id == incident_id)
                .order_by(AgentRunRecord.started_at.desc())
            ).all()
            return [self._run_to_dict(record) for record in records]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """查询单个 Agent Run。"""
        with session_scope(self.database_url) as session:
            record = session.get(AgentRunRecord, run_id)
            return self._run_to_dict(record) if record else None

    def list_timeline_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        """查询某次 Agent Run 的时间线事件。"""
        with session_scope(self.database_url) as session:
            records = session.scalars(
                select(TimelineEventRecord)
                .where(TimelineEventRecord.run_id == run_id)
                .order_by(TimelineEventRecord.created_at.asc())
            ).all()
            return [
                {
                    "time": _iso(record.created_at),
                    "type": record.event_type,
                    "stage": record.stage,
                    "message": record.message,
                    "payload": _json_loads(record.payload_json, {}),
                    "run_id": record.run_id,
                }
                for record in records
            ]

    def get_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """查询某次 Agent Run 的最新状态快照。"""
        with session_scope(self.database_url) as session:
            record = session.get(AgentStateRecord, run_id)
            if not record:
                return None
            return {
                "run_id": record.run_id,
                "incident_id": record.incident_id,
                "checkpoint_version": record.checkpoint_version,
                "updated_at": _iso(record.updated_at),
                "state": _json_loads(record.state_json, {}),
            }

    def save_state(self, run_id: str, incident_id: str, state: Dict[str, Any]) -> None:
        """保存 Agent 最新状态快照，避免服务重启后过程完全丢失。"""
        with session_scope(self.database_url) as session:
            record = session.get(AgentStateRecord, run_id)
            if record is None:
                record = AgentStateRecord(run_id=run_id, incident_id=incident_id)
                session.add(record)
            record.state_json = _json_dumps(state, MAX_PAYLOAD_CHARS)
            record.checkpoint_version = (record.checkpoint_version or 0) + 1
            record.updated_at = utc_now()

    def append_command_action(
        self,
        incident_id: str,
        action_type: str,
        actor: str,
        note: str = "",
        assignee: str | None = None,
        severity: str | None = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """追加 incident 指挥动作，形成可审计处置流。"""
        with session_scope(self.database_url) as session:
            action = IncidentCommandActionRecord(
                id=uuid.uuid4().hex,
                incident_id=incident_id,
                action_type=action_type,
                actor=actor,
                note=note,
                assignee=assignee,
                severity=severity,
                payload_json=_json_dumps(payload or {}, MAX_PAYLOAD_CHARS),
                created_at=utc_now(),
            )
            session.add(action)
            record = session.get(IncidentRecord, incident_id)
            if record:
                record.updated_at = utc_now()
            session.flush()
            return self._command_action_to_dict(action)

    def list_command_actions(self, incident_id: str) -> List[Dict[str, Any]]:
        """查询某个 incident 的全部指挥动作。"""
        with session_scope(self.database_url) as session:
            records = session.scalars(
                select(IncidentCommandActionRecord)
                .where(IncidentCommandActionRecord.incident_id == incident_id)
                .order_by(IncidentCommandActionRecord.created_at.asc())
            ).all()
            return [self._command_action_to_dict(record) for record in records]

    def _incident_to_dict(self, record: IncidentRecord) -> Dict[str, Any]:
        """把 ORM incident 转成现有 API 兼容的字典结构。"""
        return {
            "id": record.id,
            "status": record.status,
            "alert_status": record.alert_status,
            "service_name": record.service_name,
            "alert_name": record.alert_name,
            "severity": record.severity,
            "metric_name": record.metric_name,
            "threshold": record.threshold,
            "environment": record.environment,
            "description": record.description,
            "starts_at": record.starts_at,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "raw_alert": _json_loads(record.raw_alert_json, {}),
            "timeline": [
                {
                    "time": _iso(event.created_at),
                    "type": event.event_type,
                    "message": event.message,
                    "payload": _json_loads(event.payload_json, {}),
                    "run_id": event.run_id,
                }
                for event in record.timeline_events
            ],
            "command_actions": [
                self._command_action_to_dict(action)
                for action in record.command_actions
            ],
            "report": record.report or "",
        }

    def _run_to_dict(self, record: AgentRunRecord) -> Dict[str, Any]:
        """把 ORM run 转成 API 可返回的字典。"""
        return {
            "id": record.id,
            "incident_id": record.incident_id,
            "status": record.status,
            "trigger_type": record.trigger_type,
            "model_provider": record.model_provider,
            "model_name": record.model_name,
            "started_at": _iso(record.started_at),
            "ended_at": _iso(record.ended_at),
            "duration_ms": record.duration_ms,
            "total_steps": record.total_steps,
            "total_tool_calls": record.total_tool_calls,
            "total_tokens": record.total_tokens,
            "error_message": record.error_message,
            "final_report": record.final_report,
        }

    def _parse_dt(self, value: Any) -> datetime | None:
        """兼容旧代码传入的 ISO 字符串时间。"""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    def _command_action_to_dict(self, record: IncidentCommandActionRecord) -> Dict[str, Any]:
        """把 ORM 指挥动作转成 API 可返回的字典。"""
        return {
            "id": record.id,
            "incident_id": record.incident_id,
            "action_type": record.action_type,
            "actor": record.actor,
            "note": record.note,
            "assignee": record.assignee,
            "severity": record.severity,
            "payload": _json_loads(record.payload_json, {}),
            "created_at": _iso(record.created_at),
        }
