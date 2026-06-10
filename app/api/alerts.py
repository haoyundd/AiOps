"""Alertmanager webhook 和 incident 查询接口。"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.models.aiops import AIOpsRequest
from app.services.incident_service import incident_service

router = APIRouter()


@router.post("/alerts/webhook")
async def receive_alertmanager_webhook(payload: Dict[str, Any]):
    """接收 Alertmanager webhook，并自动创建 incident、启动诊断。"""
    alerts = payload.get("alerts", [])## 第 16 行：获取告警列表
    if not isinstance(alerts, list):
        raise HTTPException(status_code=400, detail="Alertmanager payload 缺少 alerts 列表")

    incidents = []
    for alert in alerts:# 第 21 行：遍历每条告警
        if not isinstance(alert, dict):
            continue
        incident = incident_service.create_or_update_from_alert(alert)# 第 25 行：创建 incident
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
