"""Structured remediation plans produced by the planner node."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from agentic_incident_remediation.models.base import StrictModel


class PlanStepKind(StrEnum):
    """High-level action categories used in remediation plans."""

    INVESTIGATE = "investigate"
    FETCH_SCHEMA = "fetch_schema"
    GENERATE = "generate"
    VALIDATE = "validate"
    REQUEST_APPROVAL = "request_approval"


class PlanStep(StrictModel):
    """One ordered action within a remediation plan."""

    id: UUID = Field(default_factory=uuid4)
    order: int = Field(ge=1)
    kind: PlanStepKind
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)


class RemediationPlan(StrictModel):
    """Typed plan that can be validated before code generation begins."""

    summary: str = Field(min_length=1, max_length=1000)
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()
