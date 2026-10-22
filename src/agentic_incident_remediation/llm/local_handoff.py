"""Bounded, typed local-model proposals for remote validation, never executable checkpoints."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from agentic_incident_remediation.models import ArtifactDraft, RemediationPlan
from agentic_incident_remediation.models.base import StrictModel, utc_now
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario

MAX_HANDOFF_BYTES = 55_000


class LocalProposal(StrictModel):
    version: Literal[1] = 1
    scenario: DemoScenario
    incident_id: UUID
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    model: Literal["qwen3:8b"] = "qwen3:8b"
    output_tokens: int = Field(default=8192, ge=128, le=8192)
    plan: RemediationPlan
    artifact: ArtifactDraft

    @model_validator(mode="after")
    def bind_proposal(self) -> "LocalProposal":
        if self.plan.incident_id != self.incident_id:
            raise ValueError("local plan belongs to another incident")
        if self.artifact.kind != SCENARIOS[self.scenario].artifact_kind:
            raise ValueError("local artifact does not match its scenario")
        return self

    def wire(self) -> str:
        content = self.model_dump_json()
        if len(content.encode()) > MAX_HANDOFF_BYTES:
            raise ValueError("local proposal exceeds GitHub's bounded handoff size")
        return content


def parse_local_proposal(content: str, scenario: DemoScenario) -> LocalProposal:
    if not content or len(content.encode()) > MAX_HANDOFF_BYTES:
        raise ValueError("invalid local proposal size")
    proposal = LocalProposal.model_validate_json(content)
    if proposal.scenario != scenario:
        raise ValueError("local proposal scenario differs from the requested case")
    return proposal
