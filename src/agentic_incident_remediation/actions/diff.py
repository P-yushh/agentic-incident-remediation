"""Compare verified proposals with explicit reference fixtures, never claimed live snapshots."""

import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from pydantic import Field

from agentic_incident_remediation.actions.models import ReviewEvidence
from agentic_incident_remediation.models.base import StrictModel
from agentic_incident_remediation.review_scenarios import SCENARIOS

BASELINES = Path(__file__).with_name("baselines")


class DiffLine(StrictModel):
    kind: Literal["context", "added", "removed", "header"]
    text: str
    old_line: int | None = None
    new_line: int | None = None


class ProposalDiff(StrictModel):
    baseline_source: Literal["synthetic_reference"] = "synthetic_reference"
    baseline_label: str = (
        "Synthetic reference baseline — not captured from this run or a real cluster"
    )
    baseline_sha256: str
    artifact_sha256: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    lines: tuple[DiffLine, ...]


def proposal_diff(review: ReviewEvidence) -> ProposalDiff:
    """Use the same exact verified proposal bytes as the full-file download."""
    if hashlib.sha256(review.content.encode()).hexdigest() != review.artifact_sha256:
        raise ValueError("cached proposal checksum does not match")
    spec = SCENARIOS[review.scenario]
    baseline = (BASELINES / f"{review.scenario.value}.{spec.extension}").read_text()
    old, new = baseline.splitlines(keepends=True), review.content.splitlines(keepends=True)
    if len(review.content) > 200_000 or len(new) > 5_000:
        raise ValueError("proposal is too large for the comparison view")
    lines: list[DiffLine] = []

    def emit(
        kind: Literal["context", "added", "removed"],
        raw: str,
        old_line: int | None,
        new_line: int | None,
    ) -> None:
        lines.append(
            DiffLine(kind=kind, text=raw.rstrip("\r\n"), old_line=old_line, new_line=new_line)
        )
        if not raw.endswith(("\n", "\r")):
            lines.append(DiffLine(kind="header", text="\\ No newline at end of file"))

    # Keep every unchanged line, not just a few lines around each edit.
    for tag, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            for index, raw in enumerate(old[a:b]):
                emit("context", raw, a + index + 1, c + index + 1)
        else:
            if tag in {"replace", "delete"}:
                for index, raw in enumerate(old[a:b]):
                    emit("removed", raw, a + index + 1, None)
            if tag in {"replace", "insert"}:
                for index, raw in enumerate(new[c:d]):
                    emit("added", raw, None, c + index + 1)
    return ProposalDiff(
        baseline_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
        artifact_sha256=review.artifact_sha256,
        additions=sum(line.kind == "added" for line in lines),
        deletions=sum(line.kind == "removed" for line in lines),
        lines=tuple(lines),
    )
