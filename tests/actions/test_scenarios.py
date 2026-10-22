"""Scenario propagation, failure projections and no-inference regression coverage."""

import copy
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agentic_incident_remediation.actions.app import create_actions_app
from agentic_incident_remediation.actions.github import (
    ActionsError,
    GitHubActionsClient,
    review_evidence,
)
from agentic_incident_remediation.actions.service import ActionsService, failure_details
from agentic_incident_remediation.actions.store import RunStore
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario


@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_each_scenario_dispatches_and_binds_review_receipt_and_download(
    tmp_path, scenario, api_factory
):
    api = api_factory(scenario)
    with httpx.Client(transport=httpx.MockTransport(api.handler)) as http:
        github = GitHubActionsClient(token="synthetic", repository=api.repository, client=http)
        service = ActionsService(github, RunStore(tmp_path / "history.db", api.repository))
        with TestClient(
            create_actions_app(service),
            base_url="http://127.0.0.1:8001",
            client=("127.0.0.1", 1234),
        ) as client:
            client.headers.update(
                {
                    "Origin": "http://127.0.0.1:8001",
                    "x-air-csrf": client.get("/api/actions/session").json()["csrf"],
                },
            )
            assert [s["id"] for s in client.get("/api/actions/scenarios").json()] == list(
                DemoScenario
            )
            identifier = str(uuid4())
            payload = {
                "request_id": identifier,
                "confirm_live_calls": True,
                "scenario": scenario.value,
            }
            assert client.post("/api/actions/runs", json=payload).status_code == 202
            assert client.post("/api/actions/runs", json=payload).status_code == 202
            writes = [r for r in api.requests if r.method == "POST"]
            assert (
                len(writes) == 1
                and json.loads(writes[0].content)["inputs"]["scenario"] == scenario.value
            )
            waiting = client.get(f"/api/actions/runs/{identifier}").json()
            assert waiting["scenario"] == waiting["review"]["scenario"] == scenario.value
            assert waiting["review"]["validation_tool"] == SCENARIOS[scenario].validator
            download = client.get(f"/api/actions/runs/{identifier}/proposal")
            assert (
                f"proposal.{SCENARIOS[scenario].extension}"
                in download.headers["content-disposition"]
            )
            if scenario is DemoScenario.CRASH_LOOP:
                assert "worker-config" in download.text and "queue-worker" in download.text
            api.stage = "completed"
            service._checked.clear()
            complete = client.get(f"/api/actions/runs/{identifier}").json()
            assert complete["receipt"]["pull_request"]["branch_name"].startswith(
                "remediation/" + scenario.value.replace("_", "-")
            )
            assert complete["receipt"]["publication_model_calls"] == 0
            other = dict(payload, scenario=next(s.value for s in DemoScenario if s != scenario))
            assert client.post("/api/actions/runs", json=other).status_code == 409
            assert len([r for r in api.requests if r.method == "POST"]) == 1


def test_unknown_scenario_and_tampered_review_cannot_dispatch_or_verify(browser, api, github):
    result = browser.post(
        "/api/actions/runs",
        json={"request_id": str(uuid4()), "confirm_live_calls": True, "scenario": "arbitrary_code"},
    )
    assert result.status_code == 422 and not api.requests
    manifest = copy.deepcopy(api.manifest)
    manifest["scenario"] = DemoScenario.TERRAFORM.value
    with pytest.raises(ValueError, match="scenario"):
        review_evidence({"manifest.json": json.dumps(manifest).encode()}, api.run(), api.repository)


def test_run_intent_rejects_other_scenario_evidence(service, api):
    record = service.start(uuid4(), DemoScenario.CRASH_LOOP)
    result = service.refresh(record.id)
    assert result.review is None and result.receipt is None
    assert "failed verification" in result.notice


def test_legacy_oom_evidence_is_readable_and_not_used_for_new_dispatch(api, github, monkeypatch):
    # Exercise historic-source caching without requiring Git history in shallow CI clones.
    import agentic_incident_remediation.actions.github as client_module

    legacy = b"historical OOM-only workflow fixture"
    monkeypatch.setattr(client_module, "LEGACY_WORKFLOW_SHA256", hashlib.sha256(legacy).hexdigest())
    api.source = legacy
    assert github.run(api.run_id)["_legacy_oom_only"] is True
    with pytest.raises(ActionsError, match="multi-scenario"):
        github.readiness()
    manifest = copy.deepcopy(api.manifest)
    manifest["version"] = 1
    manifest.pop("scenario")
    evidence = review_evidence(
        {
            "manifest.json": json.dumps(manifest).encode(),
            "proposal.yaml": manifest["state"]["artifacts"][0]["content"].encode(),
        },
        api.run(),
        api.repository,
    )
    assert evidence.scenario is DemoScenario.OOM


@pytest.mark.parametrize(
    "label,conclusion,expected",
    [
        (
            "Build real Kubernetes validator without model credentials",
            "failure",
            "before model inference",
        ),
        ("Verify required reviewers before spending model quota", "failure", "preflight"),
        (
            "Fresh alert through live inference, Docker validation and pending Redis review",
            "failure",
            "quota exhaustion is not confirmed",
        ),
        ("anything", "timed_out", "time limit"),
    ],
)
def test_failure_details_use_safe_job_step_and_reason(browser, api, label, conclusion, expected):
    api.stage = "failed"
    api.jobs_override = [
        {
            "id": 789,
            "name": "prepare",
            "conclusion": conclusion,
            "status": "completed",
            "steps": [{"name": label, "conclusion": "failure"}],
        }
    ]
    imported = browser.post("/api/actions/import", json={"run_id": api.run_id}).json()
    assert imported["failure_job"] == "prepare" and expected in imported["failure_reason"]
    assert imported["failure_url"].endswith("/job/789")
    assert not imported["review"] and not imported["receipt"]


def test_failure_metadata_does_not_echo_arbitrary_messages_or_urls():
    details = failure_details(
        [
            {
                "id": "https://attacker.invalid",
                "name": "private-token",
                "conclusion": "failure",
                "steps": [
                    {"name": "private-token https://attacker.invalid", "conclusion": "failure"}
                ],
            }
        ],
        "example/demo",
        1,
    )
    assert "private-token" not in json.dumps(details) and "attacker" not in json.dumps(details)
    assert details["failure_url"] is None
    assert failure_details([], "example/demo", 1)["failure_reason"] is None
