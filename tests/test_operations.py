"""GitHub pull-request publisher contract tests."""

import json
from base64 import b64encode
from uuid import uuid4

import httpx
import pytest

from agentic_incident_remediation.artifacts import materialize_artifact
from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.models import ArtifactDraft, PullRequestProposal
from agentic_incident_remediation.operations import GitHubPullRequestPublisher, OperationsError


def make_proposal() -> PullRequestProposal:
    artifact = materialize_artifact(
        ArtifactDraft(
            kind="kubernetes_manifest",
            path="remediations/payment-api.yaml",
            content="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: payment-api\n",
            change_summary="Add the validated payment API proposal.",
        )
    )
    return PullRequestProposal(
        execution_id=uuid4(),
        approval_id=uuid4(),
        repository="P-yushh/agentic-incident-remediation",
        base_branch="main",
        branch_name="remediation/kubernetes-oom-killed-12345678",
        title="fix(remediation): add validated proposal",
        body="Validated and human-approved proposal only.",
        commit_message="fix(remediation): add validated proposal",
        artifact=artifact,
    )


def test_github_publisher_creates_branch_commit_and_pull_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        position = len(requests)
        if position == 1:
            return httpx.Response(200, json=[])
        if position == 2:
            return httpx.Response(404, json={"message": "Not Found"})
        if position == 3:
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if position == 4:
            return httpx.Response(201, json={"ref": "created"})
        if position == 5:
            return httpx.Response(404, json={"message": "Not Found"})
        if position == 6:
            return httpx.Response(201, json={"commit": {"sha": "b" * 40}})
        return httpx.Response(
            201,
            json={
                "number": 17,
                "html_url": "https://github.com/P-yushh/agentic-incident-remediation/pull/17",
                "head": {"sha": "b" * 40},
            },
        )

    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    proposal = make_proposal()

    record = GitHubPullRequestPublisher(token="test-token", client=client).publish(proposal)

    assert record.number == 17
    assert record.commit_sha == "b" * 40
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
        "PUT",
        "POST",
    ]
    assert requests[0].headers["Authorization"] == "Bearer test-token"
    ref_payload = json.loads(requests[3].content)
    assert ref_payload == {
        "ref": f"refs/heads/{proposal.branch_name}",
        "sha": "a" * 40,
    }
    content_payload = json.loads(requests[5].content)
    assert content_payload["branch"] == proposal.branch_name
    assert content_payload["content"] == b64encode(
        proposal.artifact.content.encode("utf-8")
    ).decode("ascii")
    pr_payload = json.loads(requests[6].content)
    assert pr_payload["head"] == proposal.branch_name
    assert pr_payload["base"] == "main"


def test_github_publisher_recovers_existing_pull_request_without_duplicate_writes() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=[
                {
                    "number": 9,
                    "html_url": ("https://github.com/P-yushh/agentic-incident-remediation/pull/9"),
                    "head": {"sha": "c" * 40},
                }
            ],
        )

    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )

    record = GitHubPullRequestPublisher(token="test-token", client=client).publish(make_proposal())

    assert record.number == 9
    assert record.commit_sha == "c" * 40
    assert calls == 1


def test_github_publisher_refuses_to_overwrite_conflicting_branch_content() -> None:
    responses = iter(
        [
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"object": {"sha": "d" * 40}}),
            httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": b64encode(b"different content").decode("ascii"),
                },
            ),
        ]
    )
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(lambda _: next(responses)),
    )

    with pytest.raises(OperationsError, match="different artifact content"):
        GitHubPullRequestPublisher(token="test-token", client=client).publish(make_proposal())


def test_github_publisher_requires_credentials_and_redacts_settings() -> None:
    settings = Settings(_env_file=None, github_token=None)

    with pytest.raises(ValueError, match="GitHub token"):
        GitHubPullRequestPublisher(token=" ")
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubPullRequestPublisher.from_settings(settings)

    configured = Settings(_env_file=None, github_token="test-token")
    assert configured.safe_summary()["github_operations_configured"] is True
    assert "test-token" not in repr(configured)
    assert "github_token" not in configured.safe_summary()
