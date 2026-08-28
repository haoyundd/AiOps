import pytest

from app.domain import IncidentStatus, ensure_incident_transition


def test_incident_transition_allows_normal_diagnosis_flow():
    ensure_incident_transition(IncidentStatus.RECEIVED, IncidentStatus.DIAGNOSING)
    ensure_incident_transition(IncidentStatus.DIAGNOSING, IncidentStatus.DIAGNOSED)


def test_incident_transition_rejects_skipping_approval():
    with pytest.raises(ValueError):
        ensure_incident_transition(IncidentStatus.DIAGNOSED, IncidentStatus.REMEDIATING)
