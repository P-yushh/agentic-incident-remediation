"""LangGraph-compatible node for isolated artifact validation."""

from typing import Any

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ArtifactKind,
    MessageRole,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.sandbox.models import (
    SandboxExecution,
    SandboxRequest,
    ValidationTool,
)
from agentic_incident_remediation.sandbox.results import normalize_validation_result
from agentic_incident_remediation.sandbox.runner import SandboxError, SandboxRunner
from agentic_incident_remediation.state import add_message, record_validation, transition_state


def validation_tools_for_artifact(kind: ArtifactKind) -> tuple[ValidationTool, ...]:
    """Return the fixed validator allowlist for an artifact family."""

    if kind is ArtifactKind.KUBERNETES_MANIFEST:
        return (ValidationTool.KUBECONFORM,)
    if kind is ArtifactKind.TERRAFORM_CONFIGURATION:
        return (ValidationTool.TERRAFORM_VALIDATE,)
    raise ValueError(f"unsupported artifact kind: {kind}")


class ValidatorNode:
    """Validate the newest artifact and route deterministic outcomes safely."""

    def __init__(self, runner: SandboxRunner) -> None:
        self._runner = runner

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.VALIDATING or not state.artifacts:
            raise ValueError("validator node requires validating status and an artifact")

        artifact = state.artifacts[-1]
        working = state.model_copy(update={"validation_errors": ()})
        results: list[ValidationResult] = []
        for tool in validation_tools_for_artifact(artifact.kind):
            request = SandboxRequest(
                artifact_id=artifact.id,
                tool=tool,
                content=artifact.content,
            )
            try:
                execution = self._runner.run(request)
            except SandboxError as error:
                execution = SandboxExecution(
                    tool=tool,
                    exit_code=125,
                    stderr=str(error),
                    duration_ms=0,
                )
            result = normalize_validation_result(artifact.id, execution)
            results.append(result)
            working = record_validation(working, result)

        if any(result.status is ValidationStatus.ERROR for result in results):
            reason = "sandbox validation could not complete safely"
            working = transition_state(working, WorkflowStatus.FAILED, failure_reason=reason)
        elif any(result.status is ValidationStatus.FAILED for result in results):
            working = transition_state(working, WorkflowStatus.REFLECTING)
        else:
            working = transition_state(working, WorkflowStatus.AWAITING_APPROVAL)

        summary = ", ".join(f"{result.tool}={result.status.value}" for result in results)
        working = add_message(
            working,
            AgentMessage(
                role=MessageRole.TOOL,
                name="sandbox-validator",
                content=f"Validated {artifact.path} revision {artifact.revision}: {summary}.",
            ),
        )
        return _state_update(working)


def _state_update(state: AgentState) -> dict[str, Any]:
    return {
        "status": state.status,
        "messages": state.messages,
        "validation_results": state.validation_results,
        "validation_errors": state.validation_errors,
        "failure_reason": state.failure_reason,
        "updated_at": state.updated_at,
    }
