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
from app.services.aiops_service import aiops_service


class IncidentService:
    """管理告警 incident，并负责启动后台诊断任务。"""

    def __init__(self) -> None:
        """初始化内存存储。"""
        self._incidents: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def list_incidents(self) -> List[Dict[str, Any]]:
        """按创建时间倒序返回 incident 列表。"""
        return sorted(
            self._incidents.values(),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )

    def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        """获取单个 incident。"""
        return self._incidents.get(incident_id)

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
        incident = self._incidents.get(incident_id)
        if incident is None:
            incident = {
                "id": incident_id,
                "status": "new",
                "alert_status": alert.get("status", "firing"),
                "service_name": request.service_name,
                "alert_name": request.alert_name,
                "severity": request.severity,
                "metric_name": request.metric_name,
                "threshold": request.threshold,
                "environment": request.environment,
                "description": request.description,
                "created_at": now,
                "updated_at": now,
                "raw_alert": alert,
                "timeline": [],
                "report": "",
            }
            self._incidents[incident_id] = incident
            self._append_timeline(incident_id, "incident_created", "Alertmanager 创建 incident")
        else:
            incident.update(
                {
                    "alert_status": alert.get("status", incident.get("alert_status")),
                    "severity": request.severity,
                    "description": request.description or annotations.get("summary"),
                    "updated_at": now,
                    "raw_alert": alert,
                }
            )
            self._append_timeline(incident_id, "incident_updated", "Alertmanager 更新 incident")

        if alert.get("status", "firing") == "resolved":
            incident["status"] = "resolved"
            self._append_timeline(incident_id, "alert_resolved", "告警已恢复")
        elif config.aiops_auto_diagnosis_enabled:
            # 成本可控模式也保留开关：明确开启后，告警才会自动启动大模型诊断。
            self.start_diagnosis(incident_id, request)
        else:
            incident["status"] = "new"
            self._append_timeline(
                incident_id,
                "diagnosis_waiting_confirmation",
                "已创建 incident，自动诊断已关闭，等待用户手动点击重新诊断",
            )

        return incident

    def start_manual_diagnosis(self, request: AIOpsRequest) -> Dict[str, Any]:
        """手动诊断也生成 incident，方便前端统一展示。"""
        incident_id = request.session_id or str(uuid.uuid4())
        now = self._now()
        incident = {
            "id": incident_id,
            "status": "new",
            "alert_status": "manual",
            "service_name": request.service_name,
            "alert_name": request.alert_name,
            "severity": request.severity,
            "metric_name": request.metric_name,
            "threshold": request.threshold,
            "environment": request.environment,
            "description": request.description,
            "created_at": now,
            "updated_at": now,
            "raw_alert": {},
            "timeline": [],
            "report": "",
        }
        self._incidents[incident_id] = incident
        self._append_timeline(incident_id, "manual_created", "用户手动发起诊断")
        self.start_diagnosis(incident_id, request)
        return incident

    def start_diagnosis(self, incident_id: str, request: AIOpsRequest) -> None:
        """启动后台诊断任务，避免 Alertmanager webhook 被长时间阻塞。"""
        running_task = self._tasks.get(incident_id)
        if running_task and not running_task.done():
            logger.info(f"incident {incident_id} 诊断已在运行，跳过重复启动")
            return

        incident = self._incidents.get(incident_id)
        if incident:
            incident["status"] = "diagnosing"
            incident["updated_at"] = self._now()
            self._append_timeline(incident_id, "diagnosis_started", "Agent 自动开始诊断")

        self._tasks[incident_id] = asyncio.create_task(
            self._run_diagnosis(incident_id, request),
            name=f"incident-diagnosis-{incident_id}",
        )

    async def _run_diagnosis(self, incident_id: str, request: AIOpsRequest) -> None:
        """执行诊断并写回 incident 状态。"""
        try:
            final_report = ""
            failed_message = ""
            async for event in aiops_service.diagnose(request=request, session_id=incident_id):
                event_type = event.get("type", "unknown")
                message = event.get("message") or event.get("stage") or event_type
                self._append_timeline(incident_id, event_type, str(message), event)

                if event_type == "error":
                    failed_message = str(message)
                    break
                if event_type == "diagnosis_report":
                    final_report = event.get("report", final_report)
                elif event_type == "complete":
                    diagnosis = event.get("diagnosis", {})
                    final_report = diagnosis.get("report", final_report)

            incident = self._incidents.get(incident_id)
            if incident:
                incident["status"] = "failed" if failed_message else "diagnosed"
                incident["report"] = final_report
                incident["updated_at"] = self._now()
                if failed_message:
                    self._append_timeline(incident_id, "diagnosis_failed", failed_message)
                else:
                    self._append_timeline(incident_id, "diagnosis_completed", "诊断完成")
        except Exception as e:
            logger.error(f"incident {incident_id} 自动诊断失败: {e}", exc_info=True)
            incident = self._incidents.get(incident_id)
            if incident:
                incident["status"] = "failed"
                incident["updated_at"] = self._now()
                self._append_timeline(incident_id, "diagnosis_failed", str(e))

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
    ) -> None:
        """追加 incident 时间线事件。"""
        incident = self._incidents.get(incident_id)
        if not incident:
            return

        incident["timeline"].append(
            {
                "time": self._now(),
                "type": event_type,
                "message": message,
                "payload": payload or {},
            }
        )
        incident["updated_at"] = self._now()

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


incident_service = IncidentService()
