"""Structured remediation plans produced by the planner node."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

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

    incident_id: UUID
    summary: str = Field(min_length=1, max_length=1000)
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_execution_order(self) -> "RemediationPlan":
        """Require an ordered generation, validation, and approval lifecycle."""

        orders = [step.order for step in self.steps]
        if orders != list(range(1, len(self.steps) + 1)):
            raise ValueError("plan step orders must be contiguous and start at 1")

        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step ids must be unique")

        kinds = [step.kind for step in self.steps]
        required = (
            PlanStepKind.GENERATE,
            PlanStepKind.VALIDATE,
            PlanStepKind.REQUEST_APPROVAL,
        )
        missing = [kind.value for kind in required if kind not in kinds]
        if missing:
            raise ValueError(f"plan is missing required steps: {', '.join(missing)}")

        generate_index = kinds.index(PlanStepKind.GENERATE)
        validate_index = kinds.index(PlanStepKind.VALIDATE)
        approval_index = kinds.index(PlanStepKind.REQUEST_APPROVAL)
        if not generate_index < validate_index < approval_index:
            raise ValueError("plan must generate, validate, then request approval")
        if approval_index != len(kinds) - 1:
            raise ValueError("request approval must be the final plan step")
        return self
