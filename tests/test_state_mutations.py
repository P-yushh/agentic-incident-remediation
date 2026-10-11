"""Immutable state operation tests."""

from uuid import uuid4

import pytest

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ArtifactKind,
    GeneratedArtifact,
    Incident,
    IncidentType,
    MessageRole,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.state import (
    InvalidStateTransitionError,
    RetryLimitExceededError,
    add_artifact,
    add_message,
    increment_retry,
    record_validation,
    transition_state,
)


def make_state(**updates: object) -> AgentState:
    incident = Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Payment API pod ran out of memory",
        description="Container was terminated after exceeding its memory limit.",
    )
    return AgentState(incident=incident, **updates)


def test_transition_returns_new_state_without_mutating_original() -> None:
    state = make_state()

    updated = transition_state(state, WorkflowStatus.PLANNING)

    assert state.status is WorkflowStatus.RECEIVED
    assert updated.status is WorkflowStatus.PLANNING
    assert updated.execution_id == state.execution_id


def test_invalid_transition_is_rejected() -> None:
    state = make_state()

    with pytest.raises(InvalidStateTransitionError):
        transition_state(state, WorkflowStatus.COMPLETED)


def test_failed_transition_requires_reason() -> None:
    state = make_state()

    with pytest.raises(ValueError, match="failure_reason"):
        transition_state(state, WorkflowStatus.FAILED)


def test_messages_and_artifacts_are_appended_immutably() -> None:
    state = make_state()
    message = AgentMessage(role=MessageRole.USER, content="Investigate the incident")
    artifact = GeneratedArtifact(
        kind=ArtifactKind.KUBERNETES_MANIFEST,
        path="remediations/payment-api.yaml",
        content="apiVersion: v1",
        change_summary="Create the initial remediation proposal.",
    )

    with_message = add_message(state, message)
    with_artifact = add_artifact(with_message, artifact)

    assert state.messages == ()
    assert with_message.messages == (message,)
    assert with_artifact.artifacts == (artifact,)


def test_failed_validation_is_available_to_reflection() -> None:
    state = make_state()
    result = ValidationResult(
        artifact_id=uuid4(),
        tool="kube-linter",
        status=ValidationStatus.FAILED,
        exit_code=1,
        stderr="memory limit is missing",
        duration_ms=15,
    )

    updated = record_validation(state, result)

    assert updated.validation_results == (result,)
    assert updated.validation_errors == ("memory limit is missing",)


def test_retry_increment_stops_at_configured_limit() -> None:
    state = make_state(max_retries=1)
    retried = increment_retry(state)

    assert retried.retry_count == 1
    with pytest.raises(RetryLimitExceededError):
        increment_retry(retried)
