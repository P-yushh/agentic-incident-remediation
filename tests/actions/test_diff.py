"""Reference comparison safety, line numbers, and exact-proposal preservation."""

import hashlib
from uuid import uuid4

import pytest

from agentic_incident_remediation.actions.diff import BASELINES, proposal_diff
from agentic_incident_remediation.actions.models import ReviewEvidence
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario


def evidence(scenario, content):
    return ReviewEvidence(
        scenario=scenario,
        execution_id=uuid4(),
        approval_id=uuid4(),
        occurred_at=utc_now(),
        requested_at=utc_now(),
        artifact_path="remediations/file",
        content=content,
        artifact_sha256=hashlib.sha256(content.encode()).hexdigest(),
        validation_tool=SCENARIOS[scenario].validator,
    )


@pytest.mark.parametrize(
    "scenario,old,new",
    [
        (DemoScenario.OOM, "memory: 128Mi", "memory: 256Mi"),
        (DemoScenario.CRASH_LOOP, "name: worker-config-old", "name: worker-config"),
        (DemoScenario.TERRAFORM, '["0.0.0.0/0"]', '["10.20.0.0/16"]'),
    ],
)
def test_reference_diff_marks_real_changes_without_claiming_an_observed_baseline(
    scenario, old, new
):
    baseline = (BASELINES / f"{scenario.value}.{SCENARIOS[scenario].extension}").read_text()
    review = evidence(scenario, baseline.replace(old, new))
    result = proposal_diff(review)
    assert result.baseline_source == "synthetic_reference"
    assert "not captured" in result.baseline_label
    removed = [line for line in result.lines if line.kind == "removed"]
    added = [line for line in result.lines if line.kind == "added"]
    assert result.additions == result.deletions == 1
    assert old in removed[0].text and removed[0].old_line is not None
    assert removed[0].new_line is None
    assert new in added[0].text and added[0].new_line is not None
    assert added[0].old_line is None
    assert removed[0].old_line == added[0].new_line
    assert review.content == baseline.replace(old, new)
    assert result.artifact_sha256 == review.artifact_sha256
    # Both complete files can be recovered, including context far away from each edit.
    assert [
        line.text for line in result.lines if line.kind in {"context", "added"}
    ] == review.content.splitlines()
    assert [
        line.text for line in result.lines if line.kind in {"context", "removed"}
    ] == baseline.splitlines()
    assert not any(line.text.startswith("@@") for line in result.lines)


def test_read_only_diff_does_not_dispatch_call_models_or_change_download(browser, service, api):
    assert browser.get(f"/api/actions/runs/{uuid4()}/diff").status_code == 404
    api.stage = "queued"
    record = browser.post("/api/actions/import", json={"run_id": api.run_id}).json()
    assert browser.get(f"/api/actions/runs/{record['id']}/diff").status_code == 404
    api.stage = "awaiting_review"
    service._checked.clear()
    record = browser.get(f"/api/actions/runs/{record['id']}").json()
    before = len(api.requests)
    response = browser.get(f"/api/actions/runs/{record['id']}/diff")
    assert response.status_code == 200
    assert response.json()["artifact_sha256"] == record["review"]["artifact_sha256"]
    assert (
        browser.get(f"/api/actions/runs/{record['id']}/proposal").text
        == record["review"]["content"]
    )
    assert len(api.requests) == before
    assert not [r for r in api.requests if r.method != "GET"]


def test_no_change_newline_and_checksum_guards():
    baseline = (BASELINES / "terraform_drift.tf").read_text()
    unchanged = proposal_diff(evidence(DemoScenario.TERRAFORM, baseline))
    assert not unchanged.additions and not unchanged.deletions
    assert [line.text for line in unchanged.lines] == baseline.splitlines()
    changed = proposal_diff(evidence(DemoScenario.TERRAFORM, baseline.rstrip("\n")))
    assert any("No newline" in line.text for line in changed.lines)
    bad = evidence(DemoScenario.TERRAFORM, baseline).model_copy(update={"content": "tampered"})
    with pytest.raises(ValueError, match="checksum"):
        proposal_diff(bad)
    with pytest.raises(ValueError, match="large"):
        proposal_diff(evidence(DemoScenario.TERRAFORM, "a" * 200_001))
    with pytest.raises(ValueError, match="large"):
        proposal_diff(evidence(DemoScenario.TERRAFORM, "x\n" * 5_001))


def test_comparison_preserves_markup_as_text():
    result = proposal_diff(evidence(DemoScenario.TERRAFORM, '<script>alert("x")</script>\n'))
    assert any(line.text == '<script>alert("x")</script>' for line in result.lines)
