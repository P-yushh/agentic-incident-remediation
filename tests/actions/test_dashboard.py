"""Actions dashboard end-to-end HTTP tests, using mocked external HTTP only."""

import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentic_incident_remediation.actions.app import create_actions_app
from agentic_incident_remediation.actions.github import ActionsError
from agentic_incident_remediation.actions.models import RunRecord
from agentic_incident_remediation.actions.service import ActionsService
from agentic_incident_remediation.actions.store import RunStore


def start(browser, identifier=None):
    return browser.post(
        "/api/actions/runs",
        json={"request_id": str(identifier or uuid4()), "confirm_live_calls": True},
    )


def test_start_progress_review_publication_and_saved_downloads(browser, service, api):
    assert browser.get("/api/actions/readiness").json()["ready"]
    assert browser.get("/api/runtime").json()["mode"] == "actions"
    assert browser.get("/health").json()["status"] == "ok"
    assert "actions-dashboard.js" in browser.get("/").text
    assert "const terminal" not in browser.get("/").text  # Native controller is not loaded.
    script = browser.get("/actions-dashboard.js")
    assert script.status_code == 200 and "live-consent" in script.text
    identifier = start(browser).json()["id"]
    assert browser.get(f"/api/actions/runs/{identifier}/proposal").status_code == 404
    assert browser.get(f"/api/actions/runs/{identifier}/receipt").status_code == 404
    waiting = browser.get(f"/api/actions/runs/{identifier}").json()
    assert waiting["stage"] == "awaiting_review"
    assert waiting["review"]["model_calls"] == 2 and waiting["receipt"] is None
    assert "dump.rdb" not in json.dumps(waiting)
    proposal = browser.get(f"/api/actions/runs/{identifier}/proposal")
    assert proposal.text == api.manifest["state"]["artifacts"][0]["content"]
    assert "attachment" in proposal.headers["content-disposition"]

    api.stage = "publishing"
    service._checked.clear()
    assert browser.get(f"/api/actions/runs/{identifier}").json()["stage"] == "publishing"
    api.stage = "completed"
    service._checked.clear()
    completed = browser.get(f"/api/actions/runs/{identifier}").json()
    assert (
        completed["stage"] == "completed" and completed["receipt"]["pull_request"]["number"] == 42
    )
    receipt = browser.get(f"/api/actions/runs/{identifier}/receipt").json()
    assert receipt["execution_id"] == waiting["review"]["execution_id"]
    assert receipt["publication_model_calls"] == 0
    assert len(browser.get("/api/actions/runs").json()) == 1
    assert "test-secret" not in json.dumps(completed)
    assert [r.url.path.rsplit("/", 1)[-1] for r in api.requests if r.method == "POST"] == [
        "dispatches"
    ]


def test_approved_workspace_assets_tabs_and_no_unsupported_controls(browser):
    class ShellParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.nodes = []

        def handle_starttag(self, tag, attrs):
            self.nodes.append((tag, dict(attrs)))

    html = browser.get("/").text
    parser = ShellParser()
    parser.feed(html)
    ids = [attrs["id"] for _, attrs in parser.nodes if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert 'data-theme="sand-teal"' in html
    assert 'class="air-sidebar"' in html and 'class="air-workspace"' in html
    tabs = [attrs for _, attrs in parser.nodes if attrs.get("role") == "tab"]
    assert [tab["data-tab"] for tab in tabs] == ["investigation", "proposal", "activity"]
    assert [tab["aria-selected"] for tab in tabs] == ["false", "true", "false"]
    for tab in tabs:
        assert tab["aria-controls"] in ids
    scripts = [attrs for tag, attrs in parser.nodes if tag == "script"]
    assert scripts == [{"src": "/actions-dashboard.js", "defer": None}]
    buttons = [attrs for tag, attrs in parser.nodes if tag == "button"]
    assert not any(re.search(r"pause|resume|approve|reject", b.get("id", "")) for b in buttons)
    consent = next(attrs for _, attrs in parser.nodes if attrs.get("id") == "live-consent")
    assert "required" in consent and "checked" not in consent
    submit = next(attrs for _, attrs in parser.nodes if attrs.get("id") == "submit-incident")
    assert "disabled" in submit
    script = browser.get("/actions-dashboard.js").text
    # Every literal controller target must exist in the new shell.
    assert set(re.findall(r"byId\('([^']+)'\)", script)) <= set(ids)
    assert "innerHTML" not in script and "ArrowRight" in script
    assert "No live metrics, pod logs, or cluster events were collected" in script
    css = browser.get("/actions-dashboard.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert "--air-accent:#096b66" in css.text and "@media(max-width:600px)" in css.text


def test_duplicate_dispatch_remains_idempotent_after_recreating_service(
    browser, service, github, api
):
    identifier = uuid4()
    first = start(browser, identifier).json()
    assert start(browser, identifier).json()["id"] == first["id"]
    recreated = ActionsService(github, RunStore(service.store.path, github.repository))
    assert recreated.start(identifier).id == identifier
    assert len([r for r in api.requests if r.method == "POST"]) == 1
    assert start(browser).status_code == 409


@pytest.mark.parametrize("mode", ["network", "204", "server", "malformed", "missing"])
def test_ambiguous_dispatch_stays_locked_without_automatic_retries(browser, service, api, mode):
    if mode == "network":
        api.dispatch_error = True
    elif mode == "204":
        api.dispatch_status = 204
    elif mode == "server":
        api.dispatch_status = 500
    elif mode == "missing":
        api.dispatch_body = {}
    else:
        api.dispatch_body = {"workflow_run_id": True}
    identifier = uuid4()
    result = start(browser, identifier).json()
    assert result["stage"] == "dispatch_unknown" and result["run_id"] is None
    assert "private-token" not in json.dumps(result)
    assert start(browser, identifier).json()["stage"] == "dispatch_unknown"
    assert start(browser).status_code == 409
    assert len([r for r in api.requests if r.method == "POST"]) == 1


def test_rejected_dispatch_is_not_misreported_as_a_running_workflow(browser, api):
    api.dispatch_status = 403
    result = start(browser).json()
    assert result["stage"] == "failed" and result["run_id"] is None
    assert "403" in result["notice"]


def test_explicit_boolean_consent_required_before_any_write(browser, api):
    for value in (False, "true", 1, None):
        response = browser.post(
            "/api/actions/runs", json={"request_id": str(uuid4()), "confirm_live_calls": value}
        )
        assert response.status_code == 422
    assert not [r for r in api.requests if r.method != "GET"]


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Origin", "https://attacker.invalid"),
        ("x-air-csrf", "wrong"),
        ("Host", "attacker.invalid:8001"),
        ("Content-Type", "text/plain"),
    ],
)
def test_loopback_origin_host_nonce_and_json_guards(browser, api, header, value):
    response = browser.post(
        "/api/actions/runs",
        headers={header: value},
        json={"request_id": str(uuid4()), "confirm_live_calls": True},
    )
    assert response.status_code == 403
    assert not api.requests


def test_remote_clients_and_host_rebinding_are_rejected(service):
    with TestClient(
        create_actions_app(service), base_url="http://127.0.0.1:8001", client=("192.0.2.10", 50000)
    ) as client:
        assert client.get("/api/actions/session").status_code == 403


def test_read_only_import_history_survives_refresh_but_cannot_cancel(browser, service, api, github):
    api.stage = "completed"
    response = browser.post("/api/actions/import", json={"run_id": api.run_id})
    imported = response.json()
    assert response.status_code == 200
    assert not imported["owned"] and imported["receipt"] is not None
    assert (
        browser.post("/api/actions/import", json={"run_id": api.run_id}).json()["id"]
        == imported["id"]
    )
    assert browser.post(f"/api/actions/runs/{imported['id']}/cancel", json={}).status_code == 409
    assert len(RunStore(service.store.path, github.repository).list()) == 1
    assert not [r for r in api.requests if r.method == "POST"]


def test_cancellation_only_for_own_active_run_and_does_not_claim_completion(browser, api):
    identifier = start(browser).json()["id"]
    result = browser.post(f"/api/actions/runs/{identifier}/cancel", json={}).json()
    assert "requested" in result["notice"] and result["stage"] != "cancelled"
    assert [r for r in api.requests if r.method == "POST"][-1].url.path.endswith("/cancel")


@pytest.mark.parametrize("stage", ["queued", "preparing", "failed", "cancelled"])
def test_job_progress_and_terminal_failures_are_honest(browser, service, api, stage):
    identifier = start(browser).json()["id"]
    api.stage = stage
    result = browser.get(f"/api/actions/runs/{identifier}").json()
    assert result["stage"] == stage
    if stage == "preparing":
        assert result["steps"]
    if stage in {"failed", "cancelled"}:
        assert result["finished_at"] and result["receipt"] is None


def test_missing_or_tampered_evidence_never_becomes_a_validation_pass(browser, api):
    api.digest_override = "sha256:" + "0" * 64
    identifier = start(browser).json()["id"]
    result = browser.get(f"/api/actions/runs/{identifier}").json()
    assert result["review"] is None and result["receipt"] is None
    assert "failed verification" in result["notice"]


def test_verified_evidence_survives_remote_artifact_expiry(browser, service, api):
    identifier = start(browser).json()["id"]
    verified = browser.get(f"/api/actions/runs/{identifier}").json()["review"]
    api.expired = True
    service._checked.clear()
    result = browser.get(f"/api/actions/runs/{identifier}").json()
    assert result["review"] == verified and "expired" in result["notice"]


def test_missing_and_malformed_external_data_are_sanitized(browser, service, monkeypatch):
    assert browser.get(f"/api/actions/runs/{uuid4()}").status_code == 404
    monkeypatch.setattr(
        service.github, "readiness", Mock(side_effect=RuntimeError("private-token"))
    )
    response = browser.get("/api/actions/readiness")
    assert response.status_code == 502 and "private-token" not in response.text
    monkeypatch.setattr(
        service.github, "readiness", Mock(side_effect=ActionsError("safe lookup failure"))
    )
    assert browser.get("/api/actions/readiness").json()["detail"] == "safe lookup failure"


def test_browser_security_headers_and_no_approval_endpoint(browser):
    response = browser.get("/")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert browser.post("/api/actions/approve", json={}).status_code == 404


def test_store_repository_boundaries_and_multiple_journal_instances(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3", "example/demo")
    record = RunRecord(repository="example/demo", owned=True, stage="dispatching")
    one, inserted = store.begin(record)
    assert inserted
    two, inserted = RunStore(store.path, store.repository).begin(record)
    assert not inserted and one == two
    with pytest.raises(ValueError):
        store.save(RunRecord(repository="other/repo"))
    with pytest.raises(ValueError):
        store.begin(RunRecord(repository="other/repo"))
    assert not RunStore(store.path, "other/repo").list()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(store.path)
    with pytest.raises(ValueError, match="symbolic"):
        RunStore(link, store.repository)
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_concurrent_different_intents_only_one_can_start(github, tmp_path):
    path = tmp_path / "shared.sqlite3"
    services = [ActionsService(github, RunStore(path, github.repository)) for _ in range(2)]

    def run(service):
        try:
            return service.start(uuid4()).stage
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(run, services))
    assert results.count("blocked") == 1


def test_credentials_and_cli_never_need_browser_tokens(monkeypatch, tmp_path):
    app = importlib.import_module("agentic_incident_remediation.actions.app")
    monkeypatch.setenv("GITHUB_TOKEN", "server-test-token")
    assert app.github_credential() == "server-test-token"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setattr(
        app.subprocess,
        "run",
        Mock(
            return_value=Mock(returncode=0, stdout="username=owner\npassword=existing-test-token\n")
        ),
    )
    assert app.github_credential() == "existing-test-token"
    monkeypatch.setattr(app.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    with pytest.raises(ValueError):
        app.github_credential()
    monkeypatch.setenv("GITHUB_TOKEN", "server-test-token")
    monkeypatch.setattr("sys.argv", ["actions", "--history", str(tmp_path / "runs.sqlite3")])
    serve = Mock()
    monkeypatch.setattr(app.uvicorn, "run", serve)
    app.main()
    assert serve.call_args.kwargs["host"] == "127.0.0.1"
    monkeypatch.setattr("sys.argv", ["actions", "--port", "80"])
    with pytest.raises(SystemExit):
        app.main()
    monkeypatch.setattr("sys.argv", ["actions", "--repository", "not-a-repo"])
    with pytest.raises(SystemExit):
        app.main()


def test_service_rejects_mismatched_repository(github, tmp_path):
    with pytest.raises(ValueError):
        ActionsService(github, RunStore(tmp_path / "other.db", "other/repo"))
