"""Synthetic HTTP fixtures: no hosted models, Docker, Redis or real GitHub mutations."""

import base64
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.actions.app import create_actions_app
from agentic_incident_remediation.actions.github import WORKFLOW_PATH, GitHubActionsClient
from agentic_incident_remediation.actions.service import ActionsService
from agentic_incident_remediation.actions.store import RunStore
from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.persistence.checkpoints import create_checkpoint_serializer
from agentic_incident_remediation.review_demo import PublicationDisabled, prepare_review
from agentic_incident_remediation.review_scenarios import DemoScenario


def make_zip(files):
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class SyntheticGitHub:
    def __init__(self, scenario=DemoScenario.OOM):
        self.repository = "example/demo"
        self.sha = "a" * 40
        self.run_id = 12345
        self.stage = "awaiting_review"
        self.private = False
        self.active = True
        self.reviewers = True
        self.permissions = True
        self.source = (Path(__file__).parents[2] / WORKFLOW_PATH).read_bytes()
        self.dispatch_status = 200
        self.dispatch_body = {"workflow_run_id": self.run_id}
        self.dispatch_error = False
        self.cancel_status = 202
        self.requests = []
        self.run_override = {}
        self.expired = False
        self.digest_override = None
        self.storage_url = "https://productionresultssa0.blob.core.windows.net/data/"
        self.jobs_override = None
        self.approvals = []
        self.approvals_status = 200
        budget = BudgetedModel(FixtureStructuredModel())
        workflow = DurableApprovalWorkflow(
            budget,
            FixtureSandboxRunner(),
            PublicationDisabled(),
            repository=self.repository,
            checkpointer=InMemorySaver(serde=create_checkpoint_serializer()),
            max_retries=1,
        )
        self.manifest = prepare_review(
            workflow,
            budget,
            {
                "repository": self.repository,
                "sha": self.sha,
                "run_id": str(self.run_id),
                "run_attempt": "1",
            },
            scenario,
        )
        state = self.manifest["state"]
        self.receipt = {
            "execution_id": state["execution_id"],
            "approval_id": state["approval"]["id"],
            "reviewer": "human-reviewer",
            "artifact_sha256": self.manifest["artifact_sha256"],
            "publication_model_calls": 0,
            "status": "completed",
            "merge_performed": False,
            "deployment_performed": False,
            "pull_request": {
                "repository": self.repository,
                "number": 42,
                "url": "https://github.com/example/demo/pull/42",
                "branch_name": "remediation/"
                + scenario.value.replace("_", "-")
                + "-"
                + state["execution_id"][:8],
                "commit_sha": "b" * 40,
            },
        }
        self.custom_archives = {}

    def zip_data(self, identifier):
        if identifier in self.custom_archives:
            return self.custom_archives[identifier]
        if identifier == 1:
            return make_zip(
                {
                    "manifest.json": json.dumps(self.manifest),
                    "proposal.yaml": self.manifest["state"]["artifacts"][0]["content"],
                    "dump.rdb": b"not a real Redis checkpoint; never loaded by the dashboard",
                }
            )
        return make_zip({"publication.json": json.dumps(self.receipt)})

    def run(self):
        done = self.stage in {"completed", "failed", "cancelled"}
        return {
            "id": self.run_id,
            "head_sha": self.sha,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "run_attempt": 1,
            "path": WORKFLOW_PATH,
            "repository": {"full_name": self.repository},
            "created_at": "2026-10-18T10:00:00Z",
            "updated_at": "2026-10-18T10:02:00Z",
            "status": "completed"
            if done
            else "waiting"
            if self.stage == "awaiting_review"
            else "in_progress",
            "conclusion": "success" if self.stage == "completed" else self.stage if done else None,
        } | self.run_override

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host.endswith(".blob.core.windows.net"):
            return httpx.Response(200, content=self.zip_data(int(path.rsplit("/", 1)[-1])))
        root = "/repos/" + self.repository
        if path == root:
            return httpx.Response(200, json={"full_name": self.repository, "private": self.private})
        if path == root + "/actions/workflows/reviewed-live-demo.yml":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "state": "active" if self.active else "disabled_manually",
                    "path": WORKFLOW_PATH,
                },
            )
        if path == root + "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.sha}})
        if path == root + "/contents/" + WORKFLOW_PATH:
            return httpx.Response(200, json={"content": base64.b64encode(self.source).decode()})
        if path == root + "/environments/remediation-demo-review":
            return httpx.Response(
                200,
                json={
                    "protection_rules": [{"type": "required_reviewers", "reviewers": [{}]}]
                    if self.reviewers
                    else []
                },
            )
        if path == root + "/actions/permissions/workflow":
            return httpx.Response(200, json={"can_approve_pull_request_reviews": self.permissions})
        if path.endswith("/dispatches"):
            assert request.method == "POST"
            if self.dispatch_error:
                raise httpx.ReadTimeout("private-token-must-not-leak", request=request)
            return httpx.Response(self.dispatch_status, json=self.dispatch_body)
        if path == root + f"/actions/runs/{self.run_id}":
            return httpx.Response(200, json=self.run())
        if path.endswith("/approvals"):
            assert request.method == "GET"
            return httpx.Response(self.approvals_status, json=self.approvals)
        if path.endswith("/jobs"):
            jobs = [
                {
                    "name": "prepare",
                    "status": "in_progress" if self.stage == "preparing" else "completed",
                    "conclusion": None if self.stage == "preparing" else "success",
                    "steps": [{"name": "Live inference and validation", "status": "in_progress"}]
                    if self.stage == "preparing"
                    else [],
                },
                {
                    "name": "publish",
                    "status": "in_progress"
                    if self.stage == "publishing"
                    else "waiting"
                    if self.stage == "awaiting_review"
                    else "completed",
                    "steps": [],
                },
            ]
            return httpx.Response(
                200,
                json={
                    "jobs": self.jobs_override
                    if self.jobs_override is not None
                    else jobs
                    if self.stage != "queued"
                    else []
                },
            )
        if path.endswith("/artifacts"):
            identifiers = (
                [2, 1]
                if self.stage == "completed"
                else [1]
                if self.stage in {"publishing", "awaiting_review"}
                else []
            )
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": identifier,
                            "name": f"review-bundle-{self.run_id}-1"
                            if identifier == 1
                            else f"publication-receipt-{self.run_id}",
                            "workflow_run": {"id": self.run_id},
                            "expired": self.expired,
                            "digest": self.digest_override
                            or "sha256:" + hashlib.sha256(self.zip_data(identifier)).hexdigest(),
                        }
                        for identifier in identifiers
                    ]
                },
            )
        if path.endswith("/zip"):
            identifier = int(path.split("/")[-2])
            return httpx.Response(302, headers={"Location": self.storage_url + str(identifier)})
        if path.endswith("/cancel"):
            assert request.method == "POST"
            return httpx.Response(self.cancel_status)
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.fixture
def api_factory():
    return SyntheticGitHub


@pytest.fixture
def api():
    return SyntheticGitHub()


@pytest.fixture
def github(api):
    with httpx.Client(transport=httpx.MockTransport(api.handler)) as http:
        yield GitHubActionsClient(
            token="test-secret-not-a-real-token", repository=api.repository, client=http
        )


@pytest.fixture
def service(github, tmp_path):
    return ActionsService(github, RunStore(tmp_path / "history.sqlite3", github.repository))


@pytest.fixture
def browser(service):
    with TestClient(
        create_actions_app(service), base_url="http://127.0.0.1:8001", client=("127.0.0.1", 55000)
    ) as client:
        session = client.get("/api/actions/session").json()
        client.headers.update({"Origin": "http://127.0.0.1:8001", "x-air-csrf": session["csrf"]})
        yield client
