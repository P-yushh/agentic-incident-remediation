"""Secret-free dashboard records and verified synthetic review evidence."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StrictBool, field_validator

from agentic_incident_remediation.llm.diagnostics import FailureCode
from agentic_incident_remediation.models import PullRequestRecord
from agentic_incident_remediation.models.base import StrictModel, utc_now
from agentic_incident_remediation.review_scenarios import DemoScenario


class StartRun(StrictModel):
    request_id: UUID
    confirm_live_calls: StrictBool
    scenario: DemoScenario = DemoScenario.OOM
    generation_mode: Literal["gemini", "ollama_local"] = "gemini"
    ollama_output_tokens: int = Field(default=8192, ge=128, le=8192, strict=True)
    ollama_timeout_seconds: int = Field(default=600, ge=10, le=1200, strict=True)

    @field_validator("confirm_live_calls")
    @classmethod
    def explicit_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("explicit consent to at most two live model calls is required")
        return value


class ImportRun(StrictModel):
    run_id: int = Field(gt=0, strict=True)


class ReviewEvidence(StrictModel):
    scenario: DemoScenario = DemoScenario.OOM
    generation_mode: Literal["gemini", "ollama_local"] = "gemini"
    execution_id: UUID
    approval_id: UUID
    occurred_at: AwareDatetime
    requested_at: AwareDatetime
    artifact_path: str
    content: str
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_tool: Literal["kubeconform", "terraform_validate"] = "kubeconform"
    validation_attempts: int | None = Field(default=None, ge=1)
    validation_passes: int | None = Field(default=None, ge=1)
    retry_count: int | None = Field(default=None, ge=0)
    model_calls: Literal[2] = 2


class PublicationReceipt(StrictModel):
    execution_id: UUID
    approval_id: UUID
    reviewer: str = Field(min_length=1, max_length=200)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    publication_model_calls: Literal[0]
    pull_request: PullRequestRecord
    status: Literal["completed"]
    merge_performed: Literal[False]
    deployment_performed: Literal[False]


class ReviewRejection(StrictModel):
    reviewer: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    environment: Literal["remediation-demo-review"] = "remediation-demo-review"


class RunRecord(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    repository: str
    owned: bool = False
    scenario: DemoScenario | None = None
    generation_mode: Literal["gemini", "ollama_local"] = "gemini"
    ollama_output_tokens: int | None = None
    ollama_timeout_seconds: int | None = None
    failure_origin: Literal["local", "github"] = "github"
    failure_job: str | None = None
    failure_step: str | None = None
    failure_reason: str | None = None
    failure_url: str | None = None
    failure_code: FailureCode | None = None
    failure_phase: Literal["preparing", "publishing"] | None = None
    completed_phases: tuple[Literal["queued", "preparing", "awaiting_review"], ...] = ()
    review_rejection: ReviewRejection | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    head_sha: str = ""
    run_id: int | None = None
    run_url: str | None = None
    stage: Literal[
        "dispatching",
        "local_generating",
        "dispatch_unknown",
        "queued",
        "preparing",
        "awaiting_review",
        "publishing",
        "completed",
        "failed",
        "rejected",
        "cancelled",
    ] = "queued"
    conclusion: str | None = None
    steps: tuple[str, ...] = ()
    review: ReviewEvidence | None = None
    receipt: PublicationReceipt | None = None
    notice: str | None = None
    last_synced_at: AwareDatetime | None = None
    sync_error: str | None = None
    evidence_status: Literal["pending", "verified", "unavailable", "invalid", "expired"] = "pending"

    @property
    def active(self) -> bool:
        return self.owned and self.stage not in {"completed", "failed", "cancelled", "rejected"}
