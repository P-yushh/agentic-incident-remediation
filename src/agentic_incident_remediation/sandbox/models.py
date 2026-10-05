"""Typed requests and process results for isolated artifact validation."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from agentic_incident_remediation.models.base import StrictModel


class ValidationTool(StrEnum):
    """Allowlisted validators available inside locked-down containers."""

    KUBECONFORM = "kubeconform"
    TERRAFORM_VALIDATE = "terraform_validate"


class SandboxRequest(StrictModel):
    """One artifact validation request passed to a sandbox runner."""

    artifact_id: UUID
    tool: ValidationTool
    content: str = Field(min_length=1, max_length=200_000)


class SandboxExecution(StrictModel):
    """Raw, bounded process output captured from an isolated validator."""

    tool: ValidationTool
    exit_code: int
    stdout: str = Field(default="", max_length=20_000)
    stderr: str = Field(default="", max_length=20_000)
    duration_ms: int = Field(ge=0)
    timed_out: bool = False

    @model_validator(mode="after")
    def timeout_must_use_sentinel_exit_code(self) -> "SandboxExecution":
        if self.timed_out and self.exit_code != -1:
            raise ValueError("timed-out sandbox executions must use exit code -1")
        return self
