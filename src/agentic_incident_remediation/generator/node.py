"""LangGraph-compatible infrastructure artifact generator node."""

from typing import Any

from agentic_incident_remediation.artifacts import (
    ArtifactRevisionError,
    latest_artifact,
    materialize_artifact,
)
from agentic_incident_remediation.generator.prompts import build_generator_prompts
from agentic_incident_remediation.llm import ModelGatewayError, StructuredModel
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ArtifactDraft,
    ArtifactKind,
    IncidentType,
    MessageRole,
    WorkflowStatus,
)
from agentic_incident_remediation.schema_tools.errors import SchemaToolError
from agentic_incident_remediation.schema_tools.selection import (
    LocalSchemaToolProvider,
    SchemaToolProvider,
    schema_context_for_incident,
)
from agentic_incident_remediation.state import add_artifact, add_message, transition_state

_KUBERNETES_INCIDENTS = {
    IncidentType.KUBERNETES_OOM_KILLED,
    IncidentType.KUBERNETES_CRASH_LOOP,
}
_TERRAFORM_INCIDENTS = {
    IncidentType.TERRAFORM_DRIFT,
    IncidentType.INVALID_SECURITY_GROUP_RULE,
}


def artifact_kind_for_incident(incident_type: IncidentType) -> ArtifactKind:
    """Select the only artifact family allowed for a supported incident type."""

    if incident_type in _KUBERNETES_INCIDENTS:
        return ArtifactKind.KUBERNETES_MANIFEST
    if incident_type in _TERRAFORM_INCIDENTS:
        return ArtifactKind.TERRAFORM_CONFIGURATION
    raise ValueError(f"unsupported incident type: {incident_type}")


class GeneratorNode:
    """Generate a typed artifact and stop safely when model output is unusable."""

    def __init__(
        self,
        model: StructuredModel,
        schema_tools: SchemaToolProvider | None = None,
    ) -> None:
        self._model = model
        self._schema_tools = schema_tools or LocalSchemaToolProvider()

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.GENERATING or state.plan is None:
            raise ValueError("generator node requires generating status and a remediation plan")

        expected_kind = artifact_kind_for_incident(state.incident.type)
        try:
            schema_context = schema_context_for_incident(state.incident, self._schema_tools)
            system_prompt, user_prompt = build_generator_prompts(
                state, expected_kind, schema_context
            )
            draft = self._model.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=ArtifactDraft,
            )
            if draft.kind is not expected_kind:
                raise ModelGatewayError(
                    f"generator returned {draft.kind.value} for {state.incident.type.value}"
                )
            previous = latest_artifact(state.artifacts, expected_kind)
            artifact = materialize_artifact(draft, previous=previous)
        except (ModelGatewayError, ArtifactRevisionError, SchemaToolError) as error:
            reason = f"generator could not produce a valid remediation artifact: {error}"
            failed = transition_state(state, WorkflowStatus.FAILED, failure_reason=reason)
            failed = add_message(
                failed,
                AgentMessage(role=MessageRole.ASSISTANT, name="generator", content=reason),
            )
            return _state_update(failed, include_artifacts=False)

        generated = add_artifact(state, artifact)
        generated = add_message(
            generated,
            AgentMessage(
                role=MessageRole.ASSISTANT,
                name="generator",
                content=(
                    f"Generated {artifact.path} revision {artifact.revision} "
                    f"with checksum {artifact.checksum}."
                ),
            ),
        )
        generated = transition_state(generated, WorkflowStatus.VALIDATING)
        return _state_update(generated, include_artifacts=True)


def _state_update(state: AgentState, *, include_artifacts: bool) -> dict[str, Any]:
    update: dict[str, Any] = {
        "status": state.status,
        "messages": state.messages,
        "failure_reason": state.failure_reason,
        "updated_at": state.updated_at,
    }
    if include_artifacts:
        update["artifacts"] = state.artifacts
    return update
