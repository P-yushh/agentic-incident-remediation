"""Fail-closed source/provenance/archive checks for untrusted GitHub responses."""

import copy
import hashlib
import io
import json
import zipfile
from unittest.mock import Mock

import httpx
import pytest

from agentic_incident_remediation.actions.github import (
    WORKFLOW_SHA256,
    ActionsConnectionError,
    ActionsError,
    GitHubActionsClient,
    review_evidence,
)


def test_transport_retries_only_reads_and_does_not_expose_exception_content():
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ConnectTimeout("private-token signed-url", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        client = GitHubActionsClient(token="private-token", repository="example/demo", client=http)
        for method, count in [("GET", 2), ("POST", 1)]:
            requests.clear()
            with pytest.raises(ActionsConnectionError, match=r"GitHub API.*timed out") as error:
                client.request(method, "/repos/example/demo")
            assert len(requests) == count
            assert "private-token" not in str(error.value)
            assert "signed-url" not in str(error.value)


@pytest.mark.parametrize("failed_artifact", [1, 2])
def test_transient_storage_failure_keeps_independently_verified_proposal(
    api, github, monkeypatch, failed_artifact
):
    api.stage = "completed"

    def handler(request):
        if request.url.host != "api.github.com" and request.url.path.endswith(str(failed_artifact)):
            assert "authorization" not in request.headers
            raise httpx.ReadTimeout("signed storage URL must not leak", request=request)
        return api.handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(github, "_client", http)
        review, receipt, notice = github.evidence(github.run(api.run_id), None, None)
    assert (review is not None) == (failed_artifact == 2)
    assert receipt is None  # Never promote a receipt without its matching proposal.
    assert "GitHub artifact storage did not respond in time" in notice
    assert "signed storage URL" not in notice


def test_read_transport_recovers_on_second_attempt():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectTimeout("temporary", request=request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GitHubActionsClient(token="test", repository="example/demo", client=http)
        assert client.get("/repos/example/demo") == {"ok": True}
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("private", True),
        ("active", False),
        ("reviewers", False),
        ("permissions", False),
        ("sha", "not-a-sha"),
        ("source", b"unreviewed workflow"),
    ],
)
def test_readiness_rejects_unreviewed_or_incomplete_setup(api, github, field, value):
    setattr(api, field, value)
    with pytest.raises(ActionsError):
        github.readiness()
    assert not [r for r in api.requests if r.method == "POST"]


def test_source_pin_and_token_stay_on_api_host(api, github):
    assert hashlib.sha256(api.source).hexdigest() == WORKFLOW_SHA256
    assert github.readiness()["maximum_model_calls"] == 2
    run = github.run(api.run_id)
    review, receipt, notice = github.evidence(run, None, None)
    assert review and receipt is None and notice is None
    storage = [r for r in api.requests if r.url.host.endswith(".blob.core.windows.net")]
    assert storage and all("authorization" not in request.headers for request in storage)
    api_headers = [r.headers for r in api.requests if r.url.host == "api.github.com"]
    assert all(
        header["authorization"] == "Bearer test-secret-not-a-real-token" for header in api_headers
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_branch", "feature"),
        ("event", "pull_request"),
        ("run_attempt", 2),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "other/repo"}),
        ("head_sha", "bad"),
        ("id", 999),
    ],
)
def test_import_rejects_wrong_workflow_repository_attempt_and_branch(api, github, field, value):
    api.run_override[field] = value
    with pytest.raises(ActionsError):
        github.run(api.run_id)


def test_expected_source_revision_must_match_and_import_verifies_source(api, github):
    with pytest.raises(ActionsError):
        github.run(api.run_id, expected_sha="c" * 40)
    api.source = b"changed workflow"
    with pytest.raises(ActionsError, match="workflow has changed"):
        github.run(api.run_id)


@pytest.mark.parametrize(
    "url",
    [
        "http://productionresultssa0.blob.core.windows.net/data/",
        "https://attacker.invalid/data/",
        "https://api.github.com@attacker.invalid/data/",
        "https://user:password@productionresultssa0.blob.core.windows.net/data/",
    ],
)
def test_archive_redirect_never_sends_credential_to_arbitrary_hosts(api, github, url):
    api.storage_url = url
    with pytest.raises(ActionsError, match="redirect"):
        github.evidence(github.run(api.run_id), None, None)
    assert not [r for r in api.requests if r.url.host != "api.github.com"]


def archive_with_entries(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("mode", ["traversal", "duplicate", "zip", "oversized"])
def test_bad_archives_and_zip_bombs_are_rejected_before_extraction(api, github, mode):
    if mode == "traversal":
        raw = archive_with_entries([("../manifest.json", "{}")])
    elif mode == "duplicate":
        with pytest.warns(UserWarning):
            raw = archive_with_entries([("manifest.json", "{}"), ("manifest.json", "{}")])
    elif mode == "zip":
        raw = b"not a zip archive"
    else:
        raw = archive_with_entries([("manifest.json", "x" * 2_000_001)])
    api.custom_archives[1] = raw
    with pytest.raises(ActionsError):
        github.evidence(github.run(api.run_id), None, None)


@pytest.mark.parametrize(
    "mode", ["provenance", "budget", "state", "checksum", "content", "validation", "binding"]
)
def test_review_evidence_requires_matching_source_artifact_and_validations(api, mode):
    manifest = copy.deepcopy(api.manifest)
    proposal = manifest["state"]["artifacts"][0]["content"]
    if mode == "provenance":
        manifest["provenance"]["run_id"] = "other"
    elif mode == "budget":
        manifest["model_calls"] = 3
    elif mode == "state":
        manifest["state"]["review_ready"] = False
    elif mode == "checksum":
        manifest["artifact_sha256"] = "0" * 64
    elif mode == "content":
        proposal = "tampered proposal"
    elif mode == "validation":
        manifest["state"]["validation_results"] = []
    else:
        manifest["state"]["approval"]["artifact_ids"] = []
    with pytest.raises(ValueError if mode == "binding" else ActionsError):
        review_evidence(
            {"manifest.json": json.dumps(manifest).encode(), "proposal.yaml": proposal.encode()},
            api.run(),
            api.repository,
        )


@pytest.mark.parametrize("mode", ["repository", "url", "branch", "correlation", "model_calls"])
def test_publication_receipt_is_not_trusted_without_review_correlation(api, github, mode):
    api.stage = "completed"
    if mode == "repository":
        api.receipt["pull_request"]["repository"] = "other/repo"
    elif mode == "url":
        api.receipt["pull_request"]["url"] = "https://attacker.invalid/"
    elif mode == "branch":
        api.receipt["pull_request"]["branch_name"] = "main"
    elif mode == "correlation":
        api.receipt["artifact_sha256"] = "0" * 64
    else:
        api.receipt["publication_model_calls"] = 1
    with pytest.raises(ValueError if mode == "model_calls" else ActionsError):
        github.evidence(github.run(api.run_id), None, None)


def test_http_failures_and_malformed_json_do_not_echo_credentials():
    for status, content in [(403, b"private-token"), (200, b"not json")]:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request, code=status, body=content: httpx.Response(code, content=body)
            )
        ) as http:
            github = GitHubActionsClient(
                token="private-token", repository="example/demo", client=http
            )
            with pytest.raises(ActionsError) as error:
                github.get("/repos/example/demo")
            assert "private-token" not in str(error.value)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("private-token"))
        )
    ) as http:
        github = GitHubActionsClient(token="private-token", repository="example/demo", client=http)
        with pytest.raises(ActionsError):
            github.get("/repos/example/demo")


def test_bad_source_metadata_unavailable_artifact_and_cancel_are_safe(github, api, monkeypatch):
    original = github.get
    monkeypatch.setattr(github, "get", lambda path: {} if "/contents/" in path else original(path))
    with pytest.raises(ActionsError, match="source could not be verified"):
        github.verify_source(api.sha)
    monkeypatch.setattr(github, "get", original)
    api.cancel_status = 409
    with pytest.raises(ActionsError, match="cancellation"):
        github.cancel(api.run_id)
    monkeypatch.setattr(github, "request", Mock(return_value=httpx.Response(410)))
    with pytest.raises(ActionsError, match="expired"):
        github.archive({"id": 1})


def test_invalid_client_and_owned_client_cleanup():
    with pytest.raises(ValueError):
        GitHubActionsClient(token="", repository="example/demo")
    with pytest.raises(ValueError):
        GitHubActionsClient(token="x", repository="not/a/repo")
    github = GitHubActionsClient(token="x", repository="example/demo")
    github.close()
    assert github._client.is_closed
