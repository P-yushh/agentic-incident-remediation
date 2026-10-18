"""Typed HTTP request and health contracts."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from agentic_incident_remediation.models import (
    ApprovalDecision,
    IncidentSeverity,
    IncidentType,
)
from agentic_incident_remediation.models.base import StrictModel, utc_now


class IncidentSubmission(StrictModel):
    """Incident payload accepted by the demonstration API."""

    id: UUID = Field(default_factory=uuid4)
    type: IncidentType
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    source: str = Field(default="api", min_length=1, max_length=100)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    labels: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ApprovalSubmission(StrictModel):
    """Final human decision submitted to a paused execution."""

    approval_id: UUID
    decision: ApprovalDecision
    decided_by: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


class HealthResponse(StrictModel):
    """Liveness response that does not expose runtime secrets."""

    status: str = "ok"
    service: str = "agentic-incident-remediation"


class RuntimeInfo(StrictModel):
    """Non-secret capability labels; not a claim that dependencies are healthy."""

    mode: Literal["offline", "live", "custom"] = "custom"
    model: str = "injected"
    validators: str = "injected"
    publication: str = "injected"
    persistence: str = "injected"


class ExecutionControlSubmission(StrictModel):
    """One cooperative execution-control request."""

    action: Literal["pause", "resume", "terminate"]
