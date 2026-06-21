"""Alertmanager webhook 和 incident 查询接口。"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.models.aiops import AIOpsRequest
from app.models.incident_command import IncidentCommandActionRequest
from app.services.incident_service import incident_service

router = APIRouter()


@router.post("/alerts/webhook")
async def receive_alertmanager_webhook(payload: Dict[str, Any]):
    """接收 Alertmanager webhook，并自动创建 incident、启动诊断。"""
    alerts = payload.get("alerts", [])
    if not isinstance(alerts, list):
        raise HTTPException(status_code=400, detail="Alertmanager payload 缺少 alerts 列表")

    incidents = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        incident = incident_service.create_or_update_from_alert(alert)
        incidents.append(incident)

    logger.info(f"收到 Alertmanager webhook，处理告警数量: {len(incidents)}")
    return {
        "code": 200,
        "message": "success",
        "data": {
            "incident_count": len(incidents),
            "incidents": incidents,
        },
    }


@router.get("/incidents")
async def list_incidents():
    """查询 incident 列表。"""
    return {
        "code": 200,
        "message": "success",
        "data": incident_service.list_incidents(),
    }


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """查询单个 incident 详情。"""
    incident = incident_service.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident 不存在")

    return {
        "code": 200,
        "message": "success",
        "data": incident,
    }


@router.get("/incidents/{incident_id}/runs")
async def list_incident_runs(incident_id: str):
    """查询某个 incident 的所有 Agent Run。"""
    incident = incident_service.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident 不存在")

    return {
        "code": 200,
        "message": "success",
        "data": incident_service.repository.list_runs_for_incident(incident_id),
    }


@router.get("/incidents/{incident_id}/command")
async def get_incident_command_panel(incident_id: str):
    """查询 incident 指挥面板，包含 ICM 状态、允许动作和动作流。"""
    command_panel = incident_service.get_command_panel(incident_id)
    if not command_panel:
        raise HTTPException(status_code=404, detail="incident 不存在")

    return {
        "code": 200,
        "message": "success",
        "data": command_panel,
    }


@router.post("/incidents/{incident_id}/command/actions")
async def append_incident_command_action(incident_id: str, request: IncidentCommandActionRequest):
    """提交 incident 指挥动作，例如确认、分派、升级、缓解、恢复或重开。"""
    try:
        result = incident_service.append_command_action(
            incident_id=incident_id,
            action_type=request.action_type,
            actor=request.actor,
            note=request.note,
            assignee=request.assignee,
            severity=request.severity,
            payload=request.payload,
        )
    except ValueError as e:
        message = str(e)
        status_code = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from e

    return {
        "code": 200,
        "message": "success",
        "data": result,
    }


@router.get("/agent-runs/{run_id}")
async def get_agent_run(run_id: str):
    """查询一次 Agent Run 的概要、时间线、最新状态和审计历史。"""
    run = incident_service.repository.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="agent run 不存在")

    return {
        "code": 200,
        "message": "success",
        "data": {
            "run": run,
            "timeline": incident_service.repository.list_timeline_for_run(run_id),
            "state": incident_service.repository.get_state(run_id),
            "evaluations": incident_service.repository.list_evaluations_for_run(run_id),
        },
    }


@router.post("/incidents/{incident_id}/rediagnose")
async def rediagnose_incident(incident_id: str):
    """对已有 incident 手动重新诊断。"""
    incident = incident_service.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident 不存在")

    request = AIOpsRequest(
        session_id=incident_id,
        service_name=incident.get("service_name", "demo-service"),
        alert_name=incident.get("alert_name", "HighCPUUsage"),
        severity=incident.get("severity", "warning"),
        metric_name=incident.get("metric_name", "demo_cpu_load"),
        threshold=incident.get("threshold"),
        environment=incident.get("environment", "local"),
        description=incident.get("description"),
    )
    incident_service.start_diagnosis(incident_id, request)

    return {
        "code": 200,
        "message": "success",
        "data": incident_service.get_incident(incident_id),
    }
