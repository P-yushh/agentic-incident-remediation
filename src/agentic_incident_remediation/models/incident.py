"""Incident contracts accepted by the remediation workflow."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from agentic_incident_remediation.models.base import StrictModel, utc_now


class IncidentType(StrEnum):
    """Incident classes supported by the demonstration workflow."""

    KUBERNETES_OOM_KILLED = "kubernetes_oom_killed"
    KUBERNETES_CRASH_LOOP = "kubernetes_crash_loop"
    TERRAFORM_DRIFT = "terraform_drift"
    INVALID_SECURITY_GROUP_RULE = "invalid_security_group_rule"


class IncidentSeverity(StrEnum):
    """Operational impact assigned to an incident."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Incident(StrictModel):
    """Normalized alert consumed by the remediation workflow."""

    id: UUID = Field(default_factory=uuid4)
    type: IncidentType
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    source: str = Field(default="manual", min_length=1, max_length=100)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    labels: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        """Return the non-negative age of this incident."""

        occurred_at: datetime = self.occurred_at
        return max(0.0, (utc_now() - occurred_at).total_seconds())
