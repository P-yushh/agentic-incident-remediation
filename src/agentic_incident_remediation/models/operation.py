"""Repository-only remediation operation contracts."""

from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from agentic_incident_remediation.models.artifact import GeneratedArtifact
from agentic_incident_remediation.models.base import StrictModel, utc_now


class PullRequestProposal(StrictModel):
    """Validated repository change permitted after explicit human approval."""

    execution_id: UUID
    approval_id: UUID
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._/-]+$")
    branch_name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._/-]+$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=10_000)
    commit_message: str = Field(min_length=1, max_length=256)
    artifact: GeneratedArtifact

    @field_validator("base_branch", "branch_name")
    @classmethod
    def git_ref_cannot_be_malformed(cls, value: str) -> str:
        """Reject malformed refs before they reach a repository API."""

        if value.startswith(("/", ".")) or value.endswith(("/", ".")) or ".." in value:
            raise ValueError("git reference is malformed")
        return value


class PullRequestRecord(StrictModel):
    """Auditable result of publishing a remediation proposal."""

    repository: str
    number: int = Field(ge=1)
    url: str = Field(pattern=r"^https://")
    branch_name: str
    commit_sha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")
    created_at: AwareDatetime = Field(default_factory=utc_now)
