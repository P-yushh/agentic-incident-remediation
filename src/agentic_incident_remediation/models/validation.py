"""Sandbox validation result contracts."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from agentic_incident_remediation.models.base import StrictModel, utc_now


class ValidationStatus(StrEnum):
    """Outcome of one validation tool execution."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class FindingSeverity(StrEnum):
    """Severity assigned to a validation finding."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ValidationFinding(StrictModel):
    """One actionable issue reported by a validation tool."""

    tool: str = Field(min_length=1, max_length=100)
    severity: FindingSeverity
    message: str = Field(min_length=1, max_length=2000)
    code: str | None = Field(default=None, max_length=100)
    path: str | None = Field(default=None, max_length=300)
    line: int | None = Field(default=None, ge=1)
    remediation: str | None = Field(default=None, max_length=2000)


class ValidationResult(StrictModel):
    """Structured output captured from an isolated validation execution."""

    id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    tool: str = Field(min_length=1, max_length=100)
    status: ValidationStatus
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    findings: tuple[ValidationFinding, ...] = ()
    duration_ms: int = Field(ge=0)
    completed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def result_must_be_internally_consistent(self) -> "ValidationResult":
        """Keep process exit status aligned with the normalized outcome."""

        if self.status is ValidationStatus.PASSED and self.exit_code != 0:
            raise ValueError("passed validation must have a zero exit code")
        if self.status is ValidationStatus.FAILED and self.exit_code == 0:
            raise ValueError("failed validation must have a non-zero exit code")
        return self
