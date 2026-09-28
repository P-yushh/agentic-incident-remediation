"""Human approval contracts for proposed remediations."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from agentic_incident_remediation.models.base import StrictModel, utc_now


class ApprovalDecision(StrEnum):
    """Possible human decisions for a remediation proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRecord(StrictModel):
    """Auditable approval state for one proposed remediation."""

    id: UUID = Field(default_factory=uuid4)
    artifact_ids: tuple[UUID, ...] = Field(min_length=1)
    decision: ApprovalDecision = ApprovalDecision.PENDING
    requested_at: AwareDatetime = Field(default_factory=utc_now)
    decided_at: AwareDatetime | None = None
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def decision_metadata_must_be_consistent(self) -> "ApprovalRecord":
        """Require decision metadata only after a human has acted."""

        has_decision_metadata = self.decided_at is not None or self.decided_by is not None
        if self.decision is ApprovalDecision.PENDING and has_decision_metadata:
            raise ValueError("pending approval cannot contain decision metadata")
        if self.decision is not ApprovalDecision.PENDING and (
            self.decided_at is None or self.decided_by is None
        ):
            raise ValueError("completed approval requires decided_at and decided_by")
        return self
