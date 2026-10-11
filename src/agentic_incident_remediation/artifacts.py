"""Trusted artifact revision construction and lookup helpers."""

from collections.abc import Iterable

from agentic_incident_remediation.models import ArtifactDraft, ArtifactKind, GeneratedArtifact


class ArtifactRevisionError(ValueError):
    """Raised when a generated revision does not extend its predecessor safely."""


def latest_artifact(
    artifacts: Iterable[GeneratedArtifact], kind: ArtifactKind
) -> GeneratedArtifact | None:
    """Return the newest artifact of ``kind`` from workflow history."""

    matching = [artifact for artifact in artifacts if artifact.kind is kind]
    if not matching:
        return None
    return max(matching, key=lambda artifact: artifact.revision)


def materialize_artifact(
    draft: ArtifactDraft,
    *,
    previous: GeneratedArtifact | None = None,
) -> GeneratedArtifact:
    """Assign trusted revision metadata to a validated model draft."""

    if previous is None:
        return GeneratedArtifact(**draft.model_dump())
    if draft.kind is not previous.kind:
        raise ArtifactRevisionError("artifact revision cannot change infrastructure kind")
    if draft.path != previous.path:
        raise ArtifactRevisionError("artifact revision cannot change its repository path")
    if draft.content == previous.content:
        raise ArtifactRevisionError("artifact revision must change file content")
    return GeneratedArtifact(
        **draft.model_dump(),
        revision=previous.revision + 1,
        parent_id=previous.id,
    )
