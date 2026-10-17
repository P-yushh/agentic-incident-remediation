"""Bounded reflection node that coordinates artifact regeneration."""

from typing import Any

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    MessageRole,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.state import add_message, increment_retry, transition_state


class ReflectorNode:
    """Route actionable validation failures back to generation with a hard ceiling."""

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.REFLECTING:
            raise ValueError("reflector node requires reflecting status")
        if not state.artifacts or not state.validation_errors:
            raise ValueError("reflector node requires an artifact and validation errors")

        artifact = state.artifacts[-1]
        current_results = tuple(
            result for result in state.validation_results if result.artifact_id == artifact.id
        )
        if not current_results or not any(
            result.status is ValidationStatus.FAILED for result in current_results
        ):
            raise ValueError("reflector node requires a failed result for the latest artifact")

        if state.retry_count >= state.max_retries:
            reason = (
                f"remediation retry limit of {state.max_retries} exhausted after "
                f"artifact revision {artifact.revision} failed validation"
            )
            failed = transition_state(state, WorkflowStatus.FAILED, failure_reason=reason)
            failed = add_message(
                failed,
                AgentMessage(
                    role=MessageRole.ASSISTANT,
                    name="reflector",
                    content=reason,
                ),
            )
            return _state_update(failed)

        reflected = increment_retry(state)
        reflected = transition_state(reflected, WorkflowStatus.GENERATING)
        feedback = "\n- ".join(state.validation_errors)
        reflected = add_message(
            reflected,
            AgentMessage(
                role=MessageRole.ASSISTANT,
                name="reflector",
                content=(
                    f"Retry {reflected.retry_count}/{reflected.max_retries}: revise "
                    f"{artifact.path} without changing its path. Validation feedback:\n- "
                    f"{feedback[:4000]}"
                ),
            ),
        )
        return _state_update(reflected)


def _state_update(state: AgentState) -> dict[str, Any]:
    return {
        "status": state.status,
        "messages": state.messages,
        "retry_count": state.retry_count,
        "failure_reason": state.failure_reason,
        "updated_at": state.updated_at,
    }
