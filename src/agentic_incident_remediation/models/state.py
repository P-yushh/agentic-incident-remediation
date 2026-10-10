"""Top-level execution state shared by all workflow nodes."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from agentic_incident_remediation.models.approval import ApprovalDecision, ApprovalRecord
from agentic_incident_remediation.models.artifact import GeneratedArtifact
from agentic_incident_remediation.models.base import StrictModel, utc_now
from agentic_incident_remediation.models.incident import Incident
from agentic_incident_remediation.models.operation import PullRequestRecord
from agentic_incident_remediation.models.plan import RemediationPlan
from agentic_incident_remediation.models.validation import ValidationResult


class WorkflowStatus(StrEnum):
    """Deterministic phases of a remediation execution."""

    RECEIVED = "received"
    PLANNING = "planning"
    GENERATING = "generating"
    VALIDATING = "validating"
    REFLECTING = "reflecting"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageRole(StrEnum):
    """Message authors used in the workflow audit history."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AgentMessage(StrictModel):
    """Provider-neutral message retained in workflow state."""

    role: MessageRole
    content: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class AgentState(StrictModel):
    """Serializable source of truth passed between graph nodes."""

    execution_id: UUID = Field(default_factory=uuid4)
    incident: Incident
    status: WorkflowStatus = WorkflowStatus.RECEIVED
    messages: tuple[AgentMessage, ...] = ()
    plan: RemediationPlan | None = None
    artifacts: tuple[GeneratedArtifact, ...] = ()
    validation_results: tuple[ValidationResult, ...] = ()
    validation_errors: tuple[str, ...] = ()
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=1, le=10)
    approval: ApprovalRecord | None = None
    pull_request: PullRequestRecord | None = None
    failure_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def state_must_be_internally_consistent(self) -> "AgentState":
        """Validate cross-field workflow invariants."""

        if self.retry_count > self.max_retries:
            raise ValueError("retry_count cannot exceed max_retries")
        if self.status is WorkflowStatus.FAILED and self.failure_reason is None:
            raise ValueError("failed state requires a failure_reason")
        if self.status is not WorkflowStatus.FAILED and self.failure_reason is not None:
            raise ValueError("failure_reason is only valid for failed state")
        if self.status is WorkflowStatus.COMPLETED and self.pull_request is None:
            raise ValueError("completed state requires a pull request record")
        if self.pull_request is not None and self.status is not WorkflowStatus.COMPLETED:
            raise ValueError("pull request record is only valid for completed state")
        if self.status in {
            WorkflowStatus.APPROVED,
            WorkflowStatus.EXECUTING,
            WorkflowStatus.COMPLETED,
        } and (self.approval is None or self.approval.decision is not ApprovalDecision.APPROVED):
            raise ValueError("approved execution status requires an approved record")
        if self.status is WorkflowStatus.REJECTED and (
            self.approval is None or self.approval.decision is not ApprovalDecision.REJECTED
        ):
            raise ValueError("rejected status requires a rejected approval record")
        return self
