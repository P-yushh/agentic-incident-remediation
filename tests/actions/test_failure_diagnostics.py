"""Typed GitHub annotations, accurate phase evidence, and secret-free failure projection."""

import json

import httpx
import pytest

from agentic_incident_remediation.actions.github import ActionsError, GitHubActionsClient
from agentic_incident_remediation.llm.diagnostics import FAILURE_MESSAGES, ModelFailure


def annotation(code):
    return {
        "title": "Remediation diagnostic",
        "message": "AIR_FAILURE_V1 " + json.dumps({"code": code}),
    }


@pytest.mark.parametrize("code", list(FAILURE_MESSAGES))
def test_only_allowlisted_codes_are_projected(api, code):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[annotation(code)]))
    ) as http:
        client = GitHubActionsClient(token="test", repository=api.repository, client=http)
        result = client.failure_diagnostic(
            {"check_run_url": "https://api.github.com/repos/example/demo/check-runs/77"}
        )
    assert result == ModelFailure(code=code)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [annotation("not-a-safe-code")],
        [annotation("provider_auth"), annotation("provider_rate_limit")],
        [
            {
                "title": "Remediation diagnostic",
                "message": 'AIR_FAILURE_V1 {"code":"provider_rate_limit","secret":"never expose"}',
            }
        ],
        [{"title": "other", "message": "private-token"}],
        [None],
    ],
)
def test_arbitrary_annotation_text_and_conflicting_codes_are_not_a_diagnosis(api, payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        client = GitHubActionsClient(token="test", repository=api.repository, client=http)
        assert (
            client.failure_diagnostic(
                {"check_run_url": "https://api.github.com/repos/example/demo/check-runs/77"}
            )
            is None
        )


def test_wrong_check_url_is_never_fetched_and_metadata_errors_are_safe(api):
    def forbidden(_):
        raise AssertionError("must not fetch arbitrary URLs")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        client = GitHubActionsClient(token="test", repository=api.repository, client=http)
        for url in [
            "https://attacker.invalid/77",
            "https://api.github.com/repos/example/other/check-runs/77",
            "https://api.github.com/repos/example/demo/check-runs/77/../../secrets",
            None,
        ]:
            assert client.failure_diagnostic({"check_run_url": url}) is None
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as http:
        client = GitHubActionsClient(token="test", repository=api.repository, client=http)
        with pytest.raises(ActionsError):
            client.failure_diagnostic(
                {"check_run_url": "https://api.github.com/repos/example/demo/check-runs/77"}
            )


def test_verified_quota_marker_is_used_without_inference_or_writes(api, service, monkeypatch):
    api.stage = "failed"
    api.jobs_override = [
        {
            "name": "prepare",
            "id": 77,
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {
                    "name": (
                        "Fresh alert through live inference, Docker validation "
                        "and pending Redis review"
                    ),
                    "conclusion": "failure",
                }
            ],
        }
    ]
    monkeypatch.setattr(
        service.github, "failure_diagnostic", lambda _: ModelFailure(code="provider_rate_limit")
    )
    record = service.import_run(api.run_id)
    assert record.failure_code == "provider_rate_limit"
    assert "HTTP 429" in record.failure_reason
    assert record.failure_phase == "preparing" and record.completed_phases == ("queued",)
    assert record.review is None and record.receipt is None
    assert not [r for r in api.requests if r.method != "GET"]
    monkeypatch.setattr(
        service.github,
        "failure_diagnostic",
        lambda _: (_ for _ in ()).throw(ActionsError("unavailable")),
    )
    service._checked.clear()
    assert service.refresh(record.id).failure_code == "provider_rate_limit"
