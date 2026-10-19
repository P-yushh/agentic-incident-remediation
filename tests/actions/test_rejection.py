"""Native human rejection is not a failed validator or publisher. No external mutations."""

import pytest

from agentic_incident_remediation.actions.github import ActionsError
from agentic_incident_remediation.actions.store import RunStore


def decision(state="rejected", environment="remediation-demo-review", reviewer="human-reviewer"):
    return {"state": state, "environments": [{"name": environment}], "user": {"login": reviewer}}


def rejected_run(api):
    api.stage = "failed"
    api.run_override = {"conclusion": "failure"}
    api.jobs_override = [
        {"name": "prepare", "status": "completed", "conclusion": "success", "steps": []},
        {"name": "publish", "status": "completed", "conclusion": "failure", "steps": []},
    ]
    api.approvals = [decision()]


def test_real_review_rejection_is_terminal_read_only_and_survives_restart(api, service):
    # Cache the verified proposal first, as the real dashboard does while waiting for review.
    run = service.import_run(api.run_id)
    assert run.review
    rejected_run(api)
    service._checked.clear()
    rejected = service.refresh(run.id)
    assert rejected.stage == "rejected" and rejected.conclusion == "failure"
    assert rejected.review_rejection.reviewer == "human-reviewer"
    assert rejected.review == run.review and rejected.receipt is None
    assert rejected.finished_at and not rejected.active
    assert rejected.failure_reason is None and rejected.failure_job is None
    assert RunStore(service.store.path, api.repository).get(run.id) == rejected
    assert not [r for r in api.requests if r.method != "GET"]
    with pytest.raises(ValueError, match="active run"):
        service.cancel(run.id)


@pytest.mark.parametrize(
    "history",
    [
        [],
        [decision("approved")],
        [decision(environment="other")],
        [decision(), decision("approved")],
    ],
)
def test_empty_approved_other_environment_and_conflicting_history_are_not_rejections(
    api, service, history
):
    rejected_run(api)
    api.approvals = history
    run = service.import_run(api.run_id)
    assert run.stage == "failed" and run.review_rejection is None
    assert run.failure_reason


@pytest.mark.parametrize("mode", ["validator", "publication", "cancelled", "success"])
def test_rejection_does_not_override_technical_failure_cancellation_or_publication(
    api, service, mode
):
    rejected_run(api)
    if mode == "validator":
        api.jobs_override[0]["conclusion"] = "failure"
    elif mode == "publication":
        api.jobs_override[1]["steps"] = [{"name": "Set up job", "conclusion": "success"}]
    elif mode == "cancelled":
        api.run_override["conclusion"] = "cancelled"
    else:
        api.stage = "completed"
        api.run_override["conclusion"] = "success"
    run = service.import_run(api.run_id)
    assert run.stage != "rejected" and run.review_rejection is None
    assert not [r for r in api.requests if r.url.path.endswith("/approvals")]


def test_unavailable_history_does_not_invent_rejection_or_leak_response(api, service):
    rejected_run(api)
    api.approvals_status = 403
    api.approvals = {"message": "private response must not leak"}
    run = service.import_run(api.run_id)
    assert run.stage == "failed" and run.review_rejection is None
    assert "does not prove rejection" in run.notice
    assert "private response" not in run.model_dump_json()


def test_verified_rejection_survives_temporary_review_lookup_failure(api, service):
    rejected_run(api)
    run = service.import_run(api.run_id)
    api.approvals_status = 500
    service._checked.clear()
    refreshed = service.refresh(run.id)
    assert refreshed.stage == "rejected" and refreshed.review_rejection == run.review_rejection
    assert refreshed.notice


@pytest.mark.parametrize(
    "history",
    [
        {},
        [None],
        [{"environments": None}],
        [decision(reviewer="<script>unsafe</script>")],
        [
            {
                "state": "rejected",
                "environments": [{"name": "remediation-demo-review"}],
                "user": None,
            }
        ],
    ],
)
def test_malformed_history_and_unsafe_reviewer_are_not_trusted(api, github, history):
    api.approvals = history
    with pytest.raises(ActionsError):
        github.review_rejection(api.run_id)


def test_rejected_owned_run_releases_dispatch_lock(api, service):
    from uuid import uuid4

    original = service.start(uuid4())
    rejected_run(api)
    assert not service.refresh(original.id).active
    # No dispatch: inspect the local idempotency journal's lock only.
    from agentic_incident_remediation.actions.models import RunRecord

    _, inserted = service.store.begin(RunRecord(repository=api.repository, owned=True))
    assert inserted
