"""Pure helpers for producing validated workflow state updates."""

from collections.abc import Mapping

from agentic_incident_remediation.artifacts import ArtifactRevisionError, latest_artifact
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    GeneratedArtifact,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now


class InvalidStateTransitionError(ValueError):
    """Raised when a workflow phase transition is not permitted."""


class RetryLimitExceededError(RuntimeError):
    """Raised when another reflection attempt would exceed the configured ceiling."""


ALLOWED_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.RECEIVED: frozenset({WorkflowStatus.PLANNING, WorkflowStatus.FAILED}),
    WorkflowStatus.PLANNING: frozenset({WorkflowStatus.GENERATING, WorkflowStatus.FAILED}),
    WorkflowStatus.GENERATING: frozenset({WorkflowStatus.VALIDATING, WorkflowStatus.FAILED}),
    WorkflowStatus.VALIDATING: frozenset(
        {
            WorkflowStatus.REFLECTING,
            WorkflowStatus.AWAITING_APPROVAL,
            WorkflowStatus.FAILED,
        }
    ),
    WorkflowStatus.REFLECTING: frozenset({WorkflowStatus.GENERATING, WorkflowStatus.FAILED}),
    WorkflowStatus.AWAITING_APPROVAL: frozenset(
        {WorkflowStatus.APPROVED, WorkflowStatus.REJECTED, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.APPROVED: frozenset({WorkflowStatus.EXECUTING, WorkflowStatus.FAILED}),
    WorkflowStatus.EXECUTING: frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}),
    WorkflowStatus.REJECTED: frozenset(),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
}


def transition_state(
    state: AgentState,
    target: WorkflowStatus,
    *,
    failure_reason: str | None = None,
) -> AgentState:
    """Return a copy of state moved to an explicitly allowed phase."""

    if target not in ALLOWED_TRANSITIONS[state.status]:
        raise InvalidStateTransitionError(
            f"cannot transition workflow from {state.status.value} to {target.value}"
        )
    if target is WorkflowStatus.FAILED and not failure_reason:
        raise ValueError("failure_reason is required when transitioning to failed")
    if target is not WorkflowStatus.FAILED and failure_reason is not None:
        raise ValueError("failure_reason is only valid when transitioning to failed")

    return state.model_copy(
        update={
            "status": target,
            "failure_reason": failure_reason,
            "updated_at": utc_now(),
        }
    )


def add_message(state: AgentState, message: AgentMessage) -> AgentState:
    """Return state with one provider-neutral audit message appended."""

    return state.model_copy(
        update={"messages": (*state.messages, message), "updated_at": utc_now()}
    )


def add_artifact(state: AgentState, artifact: GeneratedArtifact) -> AgentState:
    """Append an artifact only when its revision extends state history."""

    previous = latest_artifact(state.artifacts, artifact.kind)
    if previous is None:
        if artifact.revision != 1 or artifact.parent_id is not None:
            raise ArtifactRevisionError("first artifact must be an unparented revision 1")
    elif (
        artifact.revision != previous.revision + 1
        or artifact.parent_id != previous.id
        or artifact.path != previous.path
    ):
        raise ArtifactRevisionError("artifact does not extend the latest revision")

    return state.model_copy(
        update={"artifacts": (*state.artifacts, artifact), "updated_at": utc_now()}
    )


def record_validation(state: AgentState, result: ValidationResult) -> AgentState:
    """Append a validation result and expose failed-tool messages for reflection."""

    errors = state.validation_errors
    if result.status is not ValidationStatus.PASSED:
        message = result.stderr.strip() or f"{result.tool} validation failed"
        errors = (*errors, message)

    return state.model_copy(
        update={
            "validation_results": (*state.validation_results, result),
            "validation_errors": errors,
            "updated_at": utc_now(),
        }
    )


def increment_retry(state: AgentState) -> AgentState:
    """Return state with an incremented bounded reflection counter."""

    if state.retry_count >= state.max_retries:
        raise RetryLimitExceededError(
            f"retry limit of {state.max_retries} has already been reached"
        )
    return state.model_copy(update={"retry_count": state.retry_count + 1, "updated_at": utc_now()})
