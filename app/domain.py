"""Domain enums and transition rules shared by API, worker, and persistence."""

from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class IncidentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSED = "DIAGNOSED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"


class DiagnosisStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DIAGNOSED = "DIAGNOSED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"


class EvidenceStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNAVAILABLE = "UNAVAILABLE"
    NEUTRAL = "NEUTRAL"


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class RunbookDraftStatus(StrEnum):
    """诊断复盘草稿的审核状态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


INCIDENT_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.RECEIVED: {
        IncidentStatus.DIAGNOSING,
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    },
    IncidentStatus.DIAGNOSING: {
        IncidentStatus.DIAGNOSED,
        IncidentStatus.INCONCLUSIVE,
        IncidentStatus.FAILED,
        IncidentStatus.RESOLVED,
    },
    IncidentStatus.DIAGNOSED: {
        IncidentStatus.DIAGNOSING,
        IncidentStatus.WAITING_APPROVAL,
        IncidentStatus.RESOLVED,
    },
    IncidentStatus.INCONCLUSIVE: {
        IncidentStatus.DIAGNOSING,
        IncidentStatus.RESOLVED,
    },
    IncidentStatus.FAILED: {
        IncidentStatus.DIAGNOSING,
        IncidentStatus.RESOLVED,
    },
    IncidentStatus.WAITING_APPROVAL: {
        IncidentStatus.REMEDIATING,
        IncidentStatus.DIAGNOSED,
        IncidentStatus.RESOLVED,
    },
    IncidentStatus.REMEDIATING: {
        IncidentStatus.VERIFYING,
        IncidentStatus.FAILED,
    },
    IncidentStatus.VERIFYING: {
        IncidentStatus.RESOLVED,
        IncidentStatus.FAILED,
    },
    IncidentStatus.RESOLVED: {IncidentStatus.DIAGNOSING},
}


def ensure_incident_transition(current: IncidentStatus, target: IncidentStatus) -> None:
    """Reject illegal state jumps before they can corrupt the incident timeline."""
    if current == target:
        return
    if target not in INCIDENT_TRANSITIONS[current]:
        raise ValueError(f"illegal incident transition: {current} -> {target}")
