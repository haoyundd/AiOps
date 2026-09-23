"""将高置信度诊断事实整理为待审核 Runbook 草稿。"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import DiagnosisRun, Incident, RunbookDraft
from app.domain import DiagnosisStatus, RunbookDraftStatus


def diagnosis_has_sufficient_evidence(
    conclusion: dict[str, Any], evidence: list[dict[str, Any]]
) -> bool:
    """判断诊断是否达到进入知识审核流的最低证据门槛。"""
    if conclusion.get("status") != DiagnosisStatus.DIAGNOSED.value:
        return False
    try:
        confidence = float(conclusion.get("confidence", 0))
    except (TypeError, ValueError):
        return False
    supported = [item for item in evidence if item.get("status") == "SUPPORTED"]
    # 独立性按观测证据类型判断，而不是简单按 source 判断；例如 Prometheus
    # 同时提供 JVM CPU 和 HTTP RED，仍然是两个独立信号。下一步：只有通过该门槛
    # 的事实才会进入草稿审核流，避免把重复工具调用沉淀成错误知识。
    independent_kinds = {
        str(item.get("kind")) for item in supported if item.get("kind")
    }
    return confidence >= 0.65 and len(independent_kinds) >= 2


def _draft_content(
    incident: Incident, conclusion: dict[str, Any], evidence: list[dict[str, Any]]
) -> str:
    """从诊断事实生成不包含时间戳和随机 ID 的确定性复盘文本。"""
    evidence_lines = "\n".join(
        f"- [{item.get('source')}/{item.get('kind')}] {item.get('summary', '')}"
        for item in evidence
        if item.get("status") == "SUPPORTED"
    )
    recommendations = conclusion.get("recommendations") or [
        "恢复故障依赖后重新执行健康检查和关键业务请求。"
    ]
    recommendation_lines = "\n".join(f"- {item}" for item in recommendations)
    return (
        f"# {incident.service_name} {conclusion.get('category', 'INCIDENT')} 故障复盘\n\n"
        f"## 触发条件\n{incident.alert_name}: {incident.description or '监控告警触发'}\n\n"
        f"## 根因结论\n{conclusion.get('root_cause', '')}\n\n"
        f"- 根因分类：{conclusion.get('category', '')}\n"
        f"- 诊断置信度：{float(conclusion.get('confidence', 0)):.0%}\n\n"
        f"## 关键证据\n{evidence_lines}\n\n"
        f"## 处置建议\n{recommendation_lines}\n\n"
        "## 恢复验证\n确认 Actuator 健康状态为 UP，并确认 HTTP 延迟、错误率和依赖 Span 恢复正常。\n"
    )


class RunbookDraftService:
    """负责草稿生成、查询以及确定性去重。"""

    async def create_from_diagnosis(
        self,
        session: AsyncSession,
        incident: Incident,
        run: DiagnosisRun,
        conclusion: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> RunbookDraft | None:
        """为满足门槛的诊断创建草稿，重复事实只保留一份。"""
        if not diagnosis_has_sufficient_evidence(conclusion, evidence):
            return None
        content = _draft_content(incident, conclusion, evidence)
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = await session.scalar(
            select(RunbookDraft).where(RunbookDraft.checksum == checksum)
        )
        if existing is not None:
            return cast(RunbookDraft, existing)

        category = str(conclusion.get("category", "incident")).lower()
        draft = RunbookDraft(
            incident_id=incident.id,
            diagnosis_run_id=run.id,
            title=f"{incident.service_name} {category} 故障复盘",
            service_name=incident.service_name,
            tags=[incident.service_name, category, "diagnosis-review"],
            content=content,
            checksum=checksum,
            status=RunbookDraftStatus.PENDING,
            created_by="agent",
        )
        session.add(draft)
        await session.flush()
        return draft


runbook_draft_service = RunbookDraftService()
