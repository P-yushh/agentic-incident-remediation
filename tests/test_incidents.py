"""External alert normalization tests."""

from datetime import timedelta

import pytest

from agentic_incident_remediation.incidents import (
    IncidentNormalizationError,
    normalize_incident,
)
from agentic_incident_remediation.models import Incident, IncidentSeverity, IncidentType


def test_normalize_prometheus_oom_alert() -> None:
    incident = normalize_incident(
        {
            "source": "prometheus",
            "startsAt": "2026-09-29T04:00:00Z",
            "labels": {
                "alertname": "KubePodContainerOOMKilled",
                "severity": "critical",
                "namespace": "payments",
                "pod": "payment-api-7d8f",
            },
            "annotations": {
                "summary": "Payment API container was OOMKilled",
                "description": "The container exceeded its configured memory limit.",
            },
            "fingerprint": "alert-123",
        }
    )

    assert incident.type is IncidentType.KUBERNETES_OOM_KILLED
    assert incident.severity is IncidentSeverity.CRITICAL
    assert incident.source == "prometheus"
    assert incident.labels["namespace"] == "payments"
    assert incident.context["fingerprint"] == "alert-123"
    assert incident.occurred_at.utcoffset() == timedelta(0)


def test_normalize_flat_terraform_alert_and_severity_alias() -> None:
    incident = normalize_incident(
        {
            "incident_type": "Terraform State Drift",
            "summary": "Production network drift detected",
            "message": "The current security group differs from declared state.",
            "severity": "P2",
        }
    )

    assert incident.type is IncidentType.TERRAFORM_DRIFT
    assert incident.severity is IncidentSeverity.HIGH
    assert incident.title == "Production network drift detected"


def test_normalize_returns_existing_incident_unchanged() -> None:
    incident = Incident(
        type=IncidentType.KUBERNETES_CRASH_LOOP,
        title="Worker is restarting",
        description="The worker repeatedly exits with code 1.",
    )

    assert normalize_incident(incident) is incident


@pytest.mark.parametrize(
    ("alert", "message"),
    [
        ({"type": "database_down", "title": "DB unavailable"}, "unsupported"),
        ({"type": "oomkilled"}, "title and description"),
        (
            {
                "type": "oomkilled",
                "title": "Pod failed",
                "description": "Memory exhausted",
                "severity": "catastrophic",
            },
            "unsupported incident severity",
        ),
    ],
)
def test_normalize_rejects_unsupported_or_incomplete_alerts(
    alert: dict[str, str], message: str
) -> None:
    with pytest.raises(IncidentNormalizationError, match=message):
        normalize_incident(alert)


def test_normalize_wraps_incident_validation_errors() -> None:
    with pytest.raises(IncidentNormalizationError, match="failed incident validation"):
        normalize_incident(
            {
                "type": "oomkilled",
                "title": "x" * 161,
                "description": "Memory exhausted",
            }
        )


def test_normalize_requires_mapping_input() -> None:
    with pytest.raises(IncidentNormalizationError, match="Incident or mapping"):
        normalize_incident("not an alert")  # type: ignore[arg-type]
