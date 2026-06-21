"""Incident 状态服务。

第一版使用进程内存保存 incident，适合本地演示和面试说明自动告警闭环。
生产环境可以替换为数据库或事件队列，API 层不需要大改。
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import config
from app.models.aiops import AIOpsRequest
from app.repositories.incident_repository import IncidentRepository
from app.services.aiops_service import aiops_service

COMMAND_ACTION_TYPES = {
    "acknowledge",
    "assign",
    "add_note",
    "escalate",
    "mitigate",
    "resolve",
    "reopen",
}

COMMAND_STATE_BY_ACTION = {
    "acknowledge": "acknowledged",
    "assign": "assigned",
    "escalate": "escalated",
    "mitigate": "mitigated",
    "resolve": "resolved",
    "reopen": "investigating",
}


class IncidentService:
    """管理告警 incident，并负责启动后台诊断任务。"""

    def __init__(self, repository: IncidentRepository | None = None) -> None:
        """初始化 incident 服务，使用 Repository 保存可持久化状态。"""
        self.repository = repository or IncidentRepository()
        self._tasks: Dict[str, asyncio.Task] = {}

    def list_incidents(self) -> List[Dict[str, Any]]:
        """按创建时间倒序返回 incident 列表。"""
        return [self._with_command_summary(incident) for incident in self.repository.list_incidents()]

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """获取单个 incident。"""
        incident = self.repository.get_incident(incident_id)
        return self._with_command_summary(incident) if incident else None

    def get_command_panel(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """获取 incident 指挥面板数据。"""
        incident = self.get_incident(incident_id)
        if not incident:
            return None

        return {
            "incident_id": incident_id,
            "command_state": incident["command_state"],
            "summary": incident["command_summary"],
            "actions": incident.get("command_actions", []),
            "allowed_actions": self._allowed_command_actions(incident["command_state"]),
        }

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
        """提交人工指挥动作，并按 ICM 状态机更新 incident。"""
        incident = self.get_incident(incident_id)
        if not incident:
            raise ValueError("incident 不存在")

        action_type = action_type.strip()
        self._validate_command_action(
            incident=incident,
            action_type=action_type,
            note=note,
            assignee=assignee,
            severity=severity,
        )

        action = self.repository.append_command_action(
            incident_id=incident_id,
            action_type=action_type,
            actor=actor or "operator",
            note=note,
            assignee=assignee,
            severity=severity,
            payload=payload or {},
        )

        if action_type == "resolve":
            incident["status"] = "resolved"
            incident["alert_status"] = "resolved"
            self.repository.upsert_incident(incident)
        elif action_type == "reopen":
            incident["status"] = "new"
            incident["alert_status"] = "firing"
            self.repository.upsert_incident(incident)
        elif action_type == "escalate" and severity:
            incident["severity"] = severity
            self.repository.upsert_incident(incident)

        self._append_timeline(
            incident_id,
            f"command_{action_type}",
            self._format_command_message(action_type, actor or "operator", note),
            payload={
                "action": action,
                "command_state": COMMAND_STATE_BY_ACTION.get(action_type),
            },
        )
        panel = self.get_command_panel(incident_id)
        return {
            "action": action,
            "command": panel,
            "incident": self.get_incident(incident_id),
        }

    def create_or_update_from_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """根据 Alertmanager 单条告警创建或更新 incident。"""
        labels = alert.get("labels", {})# 第 16 行：获取标签
        annotations = alert.get("annotations", {})# 第 17 行：获取注释
        #用于标识同一个告警，避免重复创建 incident，
        fingerprint = alert.get("fingerprint") or self._build_fingerprint(labels)
        incident_id = fingerprint or str(uuid.uuid4())
        now = self._now()
        # 构建 AIOPS 请求
        request = self._build_aiops_request(alert, incident_id)
        incident = self.repository.get_incident(incident_id)
        if incident is None:
            incident = {
                "id": incident_id,
                "fingerprint": fingerprint or incident_id,
                "status": "new",
                "alert_status": alert.get("status", "firing"),
                "service_name": request.service_name,
                "alert_name": request.alert_name,
                "severity": request.severity,
                "metric_name": request.metric_name,
                "threshold": request.threshold,
                "environment": request.environment,
                "description": request.description,
                "starts_at": request.starts_at,
                "created_at": now,
                "updated_at": now,
                "raw_alert": alert,
                "report": "",
            }
            incident = self.repository.upsert_incident(incident)
            self._append_timeline(incident_id, "incident_created", "Alertmanager 创建 incident")
        else:
            incident.update(
                {
                    "alert_status": alert.get("status", incident.get("alert_status")),
                    "severity": request.severity,
                    "description": request.description or annotations.get("summary"),
                    "starts_at": request.starts_at,
                    "updated_at": now,
                    "raw_alert": alert,
                }
            )
            incident = self.repository.upsert_incident(incident)
            self._append_timeline(incident_id, "incident_updated", "Alertmanager 更新 incident")

        if alert.get("status", "firing") == "resolved":
            incident["status"] = "resolved"
            self.repository.upsert_incident(incident)
            self._append_timeline(incident_id, "alert_resolved", "告警已恢复")
        elif config.aiops_auto_diagnosis_enabled:
            # 成本可控模式也保留开关：明确开启后，告警才会自动启动大模型诊断。
            self.start_diagnosis(incident_id, request)
        else:
            incident["status"] = "new"
            self.repository.upsert_incident(incident)
            self._append_timeline(
                incident_id,
                "diagnosis_waiting_confirmation",
                "已创建 incident，自动诊断已关闭，等待用户手动点击重新诊断",
            )

        return self.get_incident(incident_id) or incident

    def start_manual_diagnosis(self, request: AIOpsRequest) -> Dict[str, Any]:
        """手动诊断也生成 incident，方便前端统一展示。"""
        incident_id = request.session_id or str(uuid.uuid4())
        now = self._now()
        incident = {
            "id": incident_id,
            "fingerprint": incident_id,
            "status": "new",
            "alert_status": "manual",
            "service_name": request.service_name,
            "alert_name": request.alert_name,
            "severity": request.severity,
            "metric_name": request.metric_name,
            "threshold": request.threshold,
            "environment": request.environment,
            "description": request.description,
            "starts_at": request.starts_at,
            "created_at": now,
            "updated_at": now,
            "raw_alert": {},
            "report": "",
        }
        self.repository.upsert_incident(incident)
        self._append_timeline(incident_id, "manual_created", "用户手动发起诊断")
        self.start_diagnosis(incident_id, request, trigger_type="manual")
        return self.get_incident(incident_id) or incident

    def start_diagnosis(
        self,
        incident_id: str,
        request: AIOpsRequest,
        trigger_type: str = "webhook",
    ) -> None:
        """启动后台诊断任务，避免 Alertmanager webhook 被长时间阻塞。"""
        running_task = self._tasks.get(incident_id)
        if running_task and not running_task.done():
            logger.info(f"incident {incident_id} 诊断已在运行，跳过重复启动")
            return

        incident = self.repository.get_incident(incident_id)
        if incident:
            incident["status"] = "diagnosing"
            incident["updated_at"] = self._now()
            self.repository.upsert_incident(incident)
            run = self.repository.create_agent_run(
                incident_id=incident_id,
                trigger_type=trigger_type,
                model_provider=config.llm_provider,
                model_name=self._current_model_name(),
            )
            self._append_timeline(incident_id, "diagnosis_started", "Agent 自动开始诊断")
            run_id = run["id"]
        else:
            run_id = None

        self._tasks[incident_id] = asyncio.create_task(
            self._run_diagnosis(incident_id, request, run_id=run_id),
            name=f"incident-diagnosis-{incident_id}",
        )

    async def _run_diagnosis(
        self,
        incident_id: str,
        request: AIOpsRequest,
        run_id: str | None = None,
    ) -> None:
        """执行诊断并写回 incident 状态。"""
        if run_id is None:
            run = self.repository.create_agent_run(
                incident_id=incident_id,
                trigger_type="manual",
                model_provider=config.llm_provider,
                model_name=self._current_model_name(),
            )
            run_id = run["id"]

        try:
            final_report = ""
            failed_message = ""
            async for event in aiops_service.diagnose(request=request, session_id=incident_id):
                event_type = event.get("type", "unknown")
                message = event.get("message") or event.get("stage") or event_type
                self._append_timeline(incident_id, event_type, str(message), event, run_id=run_id)
                self.repository.save_state(
                    run_id=run_id,
                    incident_id=incident_id,
                    state={
                        "latest_event": event,
                        "event_type": event_type,
                        "message": message,
                    },
                )

                if event_type == "error":
                    failed_message = str(message)
                    break
                if event_type == "diagnosis_report":
                    final_report = event.get("report", final_report)
                elif event_type == "complete":
                    diagnosis = event.get("diagnosis", {})
                    final_report = diagnosis.get("report", final_report)

            incident = self.repository.get_incident(incident_id)
            if incident:
                incident["status"] = "failed" if failed_message else "diagnosed"
                incident["report"] = final_report
                incident["updated_at"] = self._now()
                self.repository.upsert_incident(incident)
                self.repository.finish_agent_run(
                    run_id=run_id,
                    status="failed" if failed_message else "completed",
                    final_report=final_report,
                    error_message=failed_message or None,
                )
                if failed_message:
                    self._append_timeline(
                        incident_id,
                        "diagnosis_failed",
                        failed_message,
                        run_id=run_id,
                    )
                else:
                    self._append_timeline(
                        incident_id,
                        "diagnosis_completed",
                        "诊断完成",
                        run_id=run_id,
                    )
        except Exception as e:
            logger.error(f"incident {incident_id} 自动诊断失败: {e}", exc_info=True)
            incident = self.repository.get_incident(incident_id)
            if incident:
                incident["status"] = "failed"
                incident["updated_at"] = self._now()
                self.repository.upsert_incident(incident)
                if run_id:
                    self.repository.finish_agent_run(
                        run_id=run_id,
                        status="failed",
                        error_message=str(e),
                    )
                self._append_timeline(incident_id, "diagnosis_failed", str(e), run_id=run_id)

    def _build_aiops_request(self, alert: Dict[str, Any], incident_id: str) -> AIOpsRequest:
        """从 Alertmanager 告警转换为 AIOpsRequest。"""
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        threshold_value = labels.get("threshold") or annotations.get("threshold")
        threshold = None
        if threshold_value is not None:
            try:
                threshold = float(threshold_value)
            except (TypeError, ValueError):
                threshold = None

        return AIOpsRequest(
            session_id=incident_id,
            service_name=labels.get("service", labels.get("job", "demo-service")),
            alert_name=labels.get("alertname", "UnknownAlert"),
            severity=labels.get("severity", "warning"),
            metric_name=labels.get("metric", "demo_cpu_load"),
            threshold=threshold,
            starts_at=alert.get("startsAt"),
            environment=labels.get("environment", "local"),
            description=annotations.get("description") or annotations.get("summary"),
        )
# """追加 incident 时间线事件。"""
    def _append_timeline(
        self,
        incident_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        run_id: str | None = None,
    ) -> None:
        """追加 incident 时间线事件。"""
        if not self.repository.get_incident(incident_id):
            return

        self.repository.append_timeline(
            incident_id=incident_id,
            event_type=event_type,
            message=message,
            payload=payload,
            run_id=run_id,
        )

    def _build_fingerprint(self, labels: Dict[str, Any]) -> str:
        """缺少 Alertmanager fingerprint 时生成稳定 ID。"""
        parts = [
            labels.get("alertname", "alert"),
            labels.get("service", labels.get("job", "service")),
            labels.get("environment", "local"),
        ]
        return "-".join(str(part).replace(" ", "_") for part in parts)

    def _now(self) -> str:
        """返回 UTC ISO 时间。"""
        return datetime.now(timezone.utc).isoformat()

    def _current_model_name(self) -> str:
        """返回当前推理模型名，写入 AgentRun 便于追踪诊断环境。"""
        if config.llm_model:
            return config.llm_model
        if config.llm_provider == "xiaomi":
            return config.xiaomi_model
        if config.llm_provider == "dashscope":
            return config.dashscope_model
        return config.llm_model or config.dashscope_model

    def _with_command_summary(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        """给 incident 附加从动作流推导出的 ICM 指挥状态。"""
        actions = incident.get("command_actions", [])
        summary = self._derive_command_summary(incident, actions)
        enriched = dict(incident)
        enriched["command_state"] = summary["command_state"]
        enriched["command_summary"] = summary
        return enriched

    def _derive_command_summary(
        self,
        incident: Dict[str, Any],
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """从 incident 主状态和指挥动作流推导 ICM 状态。"""
        base_state = self._base_command_state(incident.get("status", "new"))
        command_state = base_state
        assignee = None
        escalated_severity = incident.get("severity")
        last_note = ""

        for action in actions:
            action_type = action.get("action_type", "")
            command_state = COMMAND_STATE_BY_ACTION.get(action_type, command_state)
            if action.get("assignee"):
                assignee = action["assignee"]
            if action.get("severity"):
                escalated_severity = action["severity"]
            if action.get("note"):
                last_note = action["note"]

        if incident.get("status") == "resolved":
            command_state = "resolved"

        return {
            "command_state": command_state,
            "assignee": assignee,
            "severity": escalated_severity,
            "action_count": len(actions),
            "last_action": actions[-1]["action_type"] if actions else "",
            "last_note": last_note,
        }

    def _base_command_state(self, incident_status: str) -> str:
        """把原 incident.status 映射成 ICM 初始状态。"""
        if incident_status == "diagnosing":
            return "investigating"
        if incident_status == "diagnosed":
            return "diagnosed"
        if incident_status == "failed":
            return "failed"
        if incident_status == "resolved":
            return "resolved"
        return "detected"

    def _allowed_command_actions(self, command_state: str) -> List[str]:
        """根据当前 ICM 状态返回允许的人工动作。"""
        if command_state == "resolved":
            return ["add_note", "reopen"]
        if command_state == "mitigated":
            return ["add_note", "assign", "escalate", "resolve", "reopen"]
        return ["acknowledge", "assign", "add_note", "escalate", "mitigate", "resolve"]

    def _validate_command_action(
        self,
        incident: Dict[str, Any],
        action_type: str,
        note: str,
        assignee: str | None,
        severity: str | None,
    ) -> None:
        """校验指挥动作是否合法，避免 incident 被随意跳转。"""
        if action_type not in COMMAND_ACTION_TYPES:
            raise ValueError(f"不支持的指挥动作: {action_type}")

        command_state = incident.get("command_state", "detected")
        if action_type not in self._allowed_command_actions(command_state):
            raise ValueError(f"当前状态 {command_state} 不允许执行动作 {action_type}")

        if action_type == "assign" and not assignee:
            raise ValueError("assign 动作必须提供 assignee")

        if action_type == "escalate" and not severity:
            raise ValueError("escalate 动作必须提供 severity")

        if action_type == "add_note" and not note.strip():
            raise ValueError("add_note 动作必须提供 note")

    def _format_command_message(self, action_type: str, actor: str, note: str) -> str:
        """生成 timeline 中可读的指挥动作消息。"""
        action_labels = {
            "acknowledge": "确认接手 incident",
            "assign": "分派 incident",
            "add_note": "追加处置备注",
            "escalate": "升级 incident",
            "mitigate": "标记已缓解",
            "resolve": "标记已恢复",
            "reopen": "重新打开 incident",
        }
        suffix = f"：{note}" if note else ""
        return f"{actor} {action_labels.get(action_type, action_type)}{suffix}"


incident_service = IncidentService()
