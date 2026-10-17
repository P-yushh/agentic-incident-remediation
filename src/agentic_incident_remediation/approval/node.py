"""Persisted human approval request and resume handling."""

from typing import Any

from langgraph.types import interrupt

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResponse,
    MessageRole,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.state import add_message, transition_state


class PrepareApprovalNode:
    """Create one stable approval request after the latest artifact passes validation."""

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.AWAITING_APPROVAL:
            raise ValueError("approval preparation requires awaiting approval status")
        if state.approval is not None:
            raise ValueError("workflow already has an approval request")
        if not state.artifacts:
            raise ValueError("approval preparation requires a generated artifact")

        artifact = state.artifacts[-1]
        results = tuple(
            result for result in state.validation_results if result.artifact_id == artifact.id
        )
        if not results or any(result.status is not ValidationStatus.PASSED for result in results):
            raise ValueError("approval preparation requires a fully validated latest artifact")

        approval = ApprovalRecord(artifact_ids=(artifact.id,))
        prepared = state.model_copy(update={"approval": approval, "updated_at": utc_now()})
        prepared = add_message(
            prepared,
            AgentMessage(
                role=MessageRole.ASSISTANT,
                name="approval-gate",
                content=(
                    f"Approval {approval.id} requested for validated artifact "
                    f"{artifact.path} revision {artifact.revision}."
                ),
            ),
        )
        return _state_update(prepared)


class ApprovalGateNode:
    """Pause durably and accept only a response for the pending approval request."""

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.AWAITING_APPROVAL:
            raise ValueError("approval gate requires awaiting approval status")
        approval = state.approval
        if approval is None or approval.decision is not ApprovalDecision.PENDING:
            raise ValueError("approval gate requires one pending approval request")
        artifact = state.artifacts[-1]

        raw_response = interrupt(
            {
                "approval_id": str(approval.id),
                "execution_id": str(state.execution_id),
                "artifact_id": str(artifact.id),
                "artifact_path": artifact.path,
                "artifact_revision": artifact.revision,
                "artifact_checksum": artifact.checksum,
                "change_summary": artifact.change_summary,
                "requested_action": "create_pull_request",
            }
        )
        response = ApprovalResponse.model_validate(raw_response)
        if response.approval_id != approval.id:
            raise ValueError("approval response does not match the pending request")

        decided = approval.model_copy(
            update={
                "decision": response.decision,
                "decided_at": utc_now(),
                "decided_by": response.decided_by,
                "comment": response.comment,
            }
        )
        working = state.model_copy(update={"approval": decided, "updated_at": utc_now()})
        target = (
            WorkflowStatus.APPROVED
            if response.decision is ApprovalDecision.APPROVED
            else WorkflowStatus.REJECTED
        )
        working = transition_state(working, target)
        working = add_message(
            working,
            AgentMessage(
                role=MessageRole.USER,
                name="human-approver",
                content=(
                    f"Approval {approval.id} was {response.decision.value} by "
                    f"{response.decided_by}."
                ),
            ),
        )
        return _state_update(working)


def _state_update(state: AgentState) -> dict[str, Any]:
    return {
        "status": state.status,
        "messages": state.messages,
        "approval": state.approval,
        "updated_at": state.updated_at,
    }
