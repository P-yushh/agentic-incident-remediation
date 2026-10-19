"""Recover validation statistics from verified manifests, never infer missing history."""

from uuid import uuid4

import pytest

from agentic_incident_remediation.actions.github import ActionsError, review_evidence


def test_counts_are_read_from_the_actual_verified_state(api, github):
    review, _, _ = github.evidence(github.run(api.run_id), None, None)
    assert review.validation_attempts == 1
    assert review.validation_passes == 1
    assert review.retry_count == 0
    assert not [request for request in api.requests if request.method != "GET"]


def test_cached_legacy_review_is_enriched_without_changing_the_proposal(api, github):
    run = github.run(api.run_id)
    review, _, _ = github.evidence(run, None, None)
    legacy = review.model_copy(
        update={"validation_attempts": None, "validation_passes": None, "retry_count": None}
    )
    enriched, _, _ = github.evidence(run, legacy, None)
    assert enriched == review
    assert enriched.content == legacy.content
    assert enriched.artifact_sha256 == legacy.artifact_sha256


def test_missing_retry_field_is_unknown_not_assumed_zero(api):
    import json

    api.manifest["state"].pop("retry_count")
    review = review_evidence(
        {
            "manifest.json": json.dumps(api.manifest).encode(),
            "proposal.yaml": api.manifest["state"]["artifacts"][0]["content"].encode(),
        },
        api.run(),
        api.repository,
    )
    assert review.retry_count is None and review.validation_attempts == 1


def test_expired_legacy_evidence_keeps_counts_unknown(api, github):
    run = github.run(api.run_id)
    review, _, _ = github.evidence(run, None, None)
    legacy = review.model_copy(
        update={"validation_attempts": None, "validation_passes": None, "retry_count": None}
    )
    api.expired = True
    retained, _, notice = github.evidence(run, legacy, None)
    assert retained == legacy and retained.validation_attempts is None
    assert "expired" in notice


def test_backfill_cannot_replace_a_different_cached_review(api, github):
    run = github.run(api.run_id)
    review, _, _ = github.evidence(run, None, None)
    wrong = review.model_copy(update={"validation_attempts": None, "execution_id": uuid4()})
    with pytest.raises(ActionsError, match="cached proposal"):
        github.evidence(run, wrong, None)
