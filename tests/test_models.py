"""Domain model validation tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_incident_remediation.models import (
    AgentState,
    ApprovalDecision,
    ApprovalRecord,
    ArtifactKind,
    GeneratedArtifact,
    Incident,
    IncidentType,
    ValidationResult,
    ValidationStatus,
    WorkflowStatus,
)


def make_incident() -> Incident:
    return Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Payment API pod ran out of memory",
        description="Container was terminated after exceeding its memory limit.",
    )


def test_incident_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Incident(
            type=IncidentType.KUBERNETES_OOM_KILLED,
            title="Pod failed",
            description="The pod exceeded its memory limit.",
            unexpected="not allowed",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "fixes/../../secret", "."])
def test_artifact_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedArtifact(
            kind=ArtifactKind.KUBERNETES_MANIFEST,
            path=path,
            content="apiVersion: v1",
        )


def test_validation_result_requires_matching_exit_code() -> None:
    with pytest.raises(ValidationError):
        ValidationResult(
            artifact_id=uuid4(),
            tool="kube-linter",
            status=ValidationStatus.PASSED,
            exit_code=1,
            duration_ms=20,
        )


def test_completed_approval_requires_auditor_metadata() -> None:
    with pytest.raises(ValidationError):
        ApprovalRecord(
            artifact_ids=(uuid4(),),
            decision=ApprovalDecision.APPROVED,
        )


def test_completed_approval_accepts_auditor_metadata() -> None:
    record = ApprovalRecord(
        artifact_ids=(uuid4(),),
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(tz=UTC),
        decided_by="reviewer@example.com",
    )

    assert record.decision is ApprovalDecision.APPROVED


def test_failed_state_requires_reason() -> None:
    with pytest.raises(ValidationError):
        AgentState(incident=make_incident(), status=WorkflowStatus.FAILED)


def test_state_rejects_retry_count_above_limit() -> None:
    with pytest.raises(ValidationError):
        AgentState(incident=make_incident(), retry_count=4, max_retries=3)


@pytest.mark.parametrize("status", [WorkflowStatus.APPROVED, WorkflowStatus.REJECTED])
def test_decision_status_requires_matching_approval_record(status: WorkflowStatus) -> None:
    with pytest.raises(ValidationError, match="record"):
        AgentState(incident=make_incident(), status=status)
