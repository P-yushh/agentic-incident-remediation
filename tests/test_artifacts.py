"""Artifact safety and revision-history tests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_incident_remediation.artifacts import (
    ArtifactRevisionError,
    latest_artifact,
    materialize_artifact,
)
from agentic_incident_remediation.models import (
    AgentState,
    ArtifactDraft,
    ArtifactKind,
    GeneratedArtifact,
    Incident,
    IncidentType,
)
from agentic_incident_remediation.state import add_artifact


def kubernetes_draft(*, content: str = "apiVersion: apps/v1") -> ArtifactDraft:
    return ArtifactDraft(
        kind=ArtifactKind.KUBERNETES_MANIFEST,
        path="remediations/payment-api.yaml",
        content=content,
        change_summary="Increase the payment API memory limit.",
    )


def test_materialize_initial_artifact_assigns_trusted_metadata() -> None:
    artifact = materialize_artifact(kubernetes_draft())

    assert artifact.revision == 1
    assert artifact.parent_id is None
    assert len(artifact.checksum) == 64
    assert artifact.timestamp == artifact.created_at


def test_materialize_revision_links_to_previous_artifact() -> None:
    previous = materialize_artifact(kubernetes_draft())
    revision = materialize_artifact(
        kubernetes_draft(content="apiVersion: apps/v1\nkind: Deployment"),
        previous=previous,
    )

    assert revision.revision == 2
    assert revision.parent_id == previous.id
    assert latest_artifact((revision, previous), ArtifactKind.KUBERNETES_MANIFEST) == revision
    assert latest_artifact((revision,), ArtifactKind.TERRAFORM_CONFIGURATION) is None


@pytest.mark.parametrize(
    "path",
    [
        "/remediations/fix.yaml",
        "../fix.yaml",
        "remediations/../../fix.yaml",
        "remediations\\fix.yaml",
        "generated/fix.yaml",
        "remediations/.git/config.yaml",
    ],
)
def test_artifact_draft_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactDraft(
            kind=ArtifactKind.KUBERNETES_MANIFEST,
            path=path,
            content="apiVersion: v1",
            change_summary="Create a safe manifest.",
        )


@pytest.mark.parametrize(
    ("kind", "path", "message"),
    [
        (ArtifactKind.KUBERNETES_MANIFEST, "remediations/fix.tf", "Kubernetes"),
        (ArtifactKind.TERRAFORM_CONFIGURATION, "remediations/fix.yaml", "Terraform"),
    ],
)
def test_artifact_extension_must_match_kind(kind: ArtifactKind, path: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ArtifactDraft(
            kind=kind,
            path=path,
            content="resource content",
            change_summary="Create a proposed remediation.",
        )


@pytest.mark.parametrize(
    "command", ["kubectl apply -f fix.yaml", "terraform apply", "terraform destroy", "rm -rf /"]
)
def test_artifact_content_rejects_execution_commands(command: str) -> None:
    with pytest.raises(ValidationError, match="execution commands"):
        ArtifactDraft(
            kind=ArtifactKind.KUBERNETES_MANIFEST,
            path="remediations/fix.yaml",
            content=command,
            change_summary="Unsafe output.",
        )


def test_materialize_rejects_incompatible_or_empty_revision() -> None:
    previous = materialize_artifact(kubernetes_draft())

    with pytest.raises(ArtifactRevisionError, match="kind"):
        materialize_artifact(
            ArtifactDraft(
                kind=ArtifactKind.TERRAFORM_CONFIGURATION,
                path="remediations/payment-api.tf",
                content='resource "example" "fix" {}',
                change_summary="Attempt to change artifact kind.",
            ),
            previous=previous,
        )
    with pytest.raises(ArtifactRevisionError, match="path"):
        materialize_artifact(
            kubernetes_draft(content="apiVersion: v1").model_copy(
                update={"path": "remediations/renamed.yaml"}
            ),
            previous=previous,
        )
    with pytest.raises(ArtifactRevisionError, match="change file content"):
        materialize_artifact(kubernetes_draft(), previous=previous)


def test_generated_artifact_parent_rules_are_enforced() -> None:
    draft = kubernetes_draft().model_dump()

    with pytest.raises(ValidationError, match="cannot have a parent"):
        GeneratedArtifact(**draft, parent_id=uuid4())
    with pytest.raises(ValidationError, match="require a parent"):
        GeneratedArtifact(**draft, revision=2)


def test_state_rejects_artifact_that_skips_revision_history() -> None:
    first = materialize_artifact(kubernetes_draft())
    skipped = GeneratedArtifact(
        **kubernetes_draft(content="apiVersion: v1").model_dump(),
        revision=3,
        parent_id=first.id,
    )
    state = AgentState(
        incident=Incident(
            type=IncidentType.KUBERNETES_OOM_KILLED,
            title="Pod exceeded memory",
            description="The pod was terminated after exceeding its memory limit.",
        ),
        artifacts=(first,),
    )

    with pytest.raises(ArtifactRevisionError, match="latest revision"):
        add_artifact(state, skipped)
