"""Post-approval executor restricted to publishing a pull request proposal."""

from typing import Any

from pydantic import ValidationError

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ApprovalDecision,
    MessageRole,
    PullRequestProposal,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.operations.publisher import OperationsError, PullRequestPublisher
from agentic_incident_remediation.state import add_message, transition_state


class PullRequestExecutorNode:
    """Publish the validated artifact without any infrastructure apply capability."""

    def __init__(
        self,
        publisher: PullRequestPublisher,
        *,
        repository: str,
        base_branch: str = "main",
    ) -> None:
        self._publisher = publisher
        self._repository = repository
        self._base_branch = base_branch

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.APPROVED:
            raise ValueError("pull request executor requires approved status")
        approval = state.approval
        if approval is None or approval.decision is not ApprovalDecision.APPROVED:
            raise ValueError("pull request executor requires an approved record")
        if not state.artifacts or state.artifacts[-1].id not in approval.artifact_ids:
            raise ValueError("approval does not cover the latest artifact")

        working = transition_state(state, WorkflowStatus.EXECUTING)
        try:
            proposal = build_pull_request_proposal(
                working,
                repository=self._repository,
                base_branch=self._base_branch,
            )
            pull_request = self._publisher.publish(proposal)
            if (
                pull_request.repository != proposal.repository
                or pull_request.branch_name != proposal.branch_name
            ):
                raise OperationsError("publisher returned a record for a different proposal")
        except (OperationsError, ValidationError):
            reason = "approved pull request publication failed safely"
            failed = transition_state(working, WorkflowStatus.FAILED, failure_reason=reason)
            failed = add_message(
                failed,
                AgentMessage(
                    role=MessageRole.TOOL,
                    name="repository-operations",
                    content=reason,
                ),
            )
            return _state_update(failed)

        completed = AgentState.model_validate(
            working.model_dump()
            | {
                "status": WorkflowStatus.COMPLETED,
                "pull_request": pull_request,
                "updated_at": utc_now(),
            }
        )
        completed = add_message(
            completed,
            AgentMessage(
                role=MessageRole.TOOL,
                name="repository-operations",
                content=f"Created remediation pull request {pull_request.url}.",
            ),
        )
        return _state_update(completed)


def build_pull_request_proposal(
    state: AgentState,
    *,
    repository: str,
    base_branch: str,
) -> PullRequestProposal:
    """Build a deterministic, replay-safe repository proposal from approved state."""

    approval = state.approval
    if approval is None or approval.decided_by is None:
        raise ValueError("pull request proposal requires approval metadata")
    artifact = state.artifacts[-1]
    incident_slug = state.incident.type.value.replace("_", "-")
    branch_name = f"remediation/{incident_slug}-{str(state.execution_id)[:8]}"
    summary = artifact.change_summary.rstrip(".")
    return PullRequestProposal(
        execution_id=state.execution_id,
        approval_id=approval.id,
        repository=repository,
        base_branch=base_branch,
        branch_name=branch_name,
        title=f"fix(remediation): {summary}",
        body=(
            "## Incident\n\n"
            f"{state.incident.title}\n\n"
            "## Validated proposal\n\n"
            f"- Artifact: `{artifact.path}`\n"
            f"- Revision: {artifact.revision}\n"
            f"- Checksum: `{artifact.checksum}`\n"
            f"- Approved by: {approval.decided_by}\n\n"
            "This pull request is a proposal only. No Kubernetes or Terraform apply command "
            "was executed."
        ),
        commit_message=f"fix(remediation): {summary}",
        artifact=artifact,
    )


def _state_update(state: AgentState) -> dict[str, Any]:
    return {
        "status": state.status,
        "messages": state.messages,
        "pull_request": state.pull_request,
        "failure_reason": state.failure_reason,
        "updated_at": state.updated_at,
    }
