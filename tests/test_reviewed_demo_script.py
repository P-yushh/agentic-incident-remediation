"""Offline safety tests for the manual Actions demo and review-bundle handoff."""

import importlib.util
import json
import os
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.llm import GeminiStructuredModel
from agentic_incident_remediation.models import ArtifactDraft, RemediationPlan
from agentic_incident_remediation.operations import GitHubPullRequestPublisher
from agentic_incident_remediation.persistence.checkpoints import create_checkpoint_serializer
from agentic_incident_remediation.review_scenarios import DemoScenario

SCRIPT = Path(__file__).parents[1] / "scripts" / "reviewed_live_demo.py"
SPEC = importlib.util.spec_from_file_location("reviewed_live_demo", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@pytest.fixture
def github_context(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"private": False, "full_name": "example/demo"}}))
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "example/demo",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": (
            "example/demo/.github/workflows/reviewed-live-demo.yml@refs/heads/main"
        ),
        "GITHUB_EVENT_PATH": str(event),
        "AIR_CONFIRM_LIVE_DEMO": "true",
    }.items():
        monkeypatch.setenv(name, value)
    return demo.context()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_REF", "refs/heads/topic"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("AIR_CONFIRM_LIVE_DEMO", "false"),
        ("GITHUB_SHA", "not-a-sha"),
        ("GITHUB_REPOSITORY", "other/repository"),
        ("GITHUB_WORKFLOW_REF", "example/demo/.github/workflows/other.yml@refs/heads/main"),
    ],
)
def test_rejects_untrusted_context(monkeypatch, github_context, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="first-attempt"):
        demo.context()


def test_rejects_private_repository(github_context):
    event = Path(os.environ["GITHUB_EVENT_PATH"])
    payload = json.loads(event.read_text())
    payload["repository"]["private"] = True
    event.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        demo.context()


def environment():
    return {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [
            {
                "state": "rejected",
                "environments": [{"name": demo.ENVIRONMENT}],
                "user": {"type": "User"},
            }
        ],
        [
            {
                "state": "approved",
                "environments": [{"name": demo.ENVIRONMENT}],
                "user": {"type": "Bot"},
            }
        ],
        [{"state": "approved", "environments": [{"name": "other"}], "user": {"type": "User"}}],
    ],
)
def test_bypass_missing_or_nonhuman_review_cannot_publish(monkeypatch, reviews):
    monkeypatch.setattr(demo, "github_get", Mock(side_effect=[environment(), reviews]))
    with pytest.raises(ValueError):
        demo.approved_reviewer({"repository": "example/demo", "run_id": "12345"})


def test_environment_protection_and_actual_human_record(monkeypatch):
    monkeypatch.setattr(demo, "github_get", Mock(return_value={"protection_rules": []}))
    with pytest.raises(ValueError, match="required reviewer"):
        demo.require_review_environment({"repository": "example/demo"})
    monkeypatch.setattr(
        demo,
        "github_get",
        Mock(
            side_effect=[
                environment(),
                [
                    {
                        "state": "approved",
                        "environments": [{"name": demo.ENVIRONMENT}],
                        "user": {"type": "User", "login": "human"},
                    }
                ],
            ]
        ),
    )
    assert demo.approved_reviewer({"repository": "example/demo", "run_id": "12345"}) == "human"


def make_bundle(directory, provenance):
    manifest = {"provenance": provenance, "state": {"artifacts": [{"content": "synthetic YAML"}]}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "proposal.yaml").write_text("synthetic YAML")
    (directory / "dump.rdb").write_bytes(b"synthetic RDB")
    return demo.bundle_digest(directory)


def test_bundle_bound_to_original_run_commit_and_exact_files(tmp_path):
    provenance = {"run_id": "12345", "sha": "a" * 40}
    digest = make_bundle(tmp_path, provenance)
    assert demo.verify_bundle(tmp_path, provenance, digest)["provenance"] == provenance
    with pytest.raises(ValueError, match="different run"):
        demo.verify_bundle(tmp_path, {"run_id": "67890"}, digest)
    with pytest.raises(ValueError, match="digest is required"):
        demo.verify_bundle(tmp_path, provenance, "")
    (tmp_path / "proposal.yaml").write_text("changed YAML")
    with pytest.raises(ValueError, match="digest changed"):
        demo.verify_bundle(tmp_path, provenance, digest)
    with pytest.raises(ValueError, match="proposal differs"):
        demo.verify_bundle(tmp_path, provenance, demo.bundle_digest(tmp_path))


def test_bundle_rejects_symlinks_and_missing_files(tmp_path):
    make_bundle(tmp_path, {})
    (tmp_path / "dump.rdb").unlink()
    with pytest.raises(ValueError, match="invalid review"):
        demo.bundle_digest(tmp_path)
    (tmp_path / "dump.rdb").symlink_to(tmp_path / "proposal.yaml")
    with pytest.raises(ValueError, match="invalid review"):
        demo.bundle_digest(tmp_path)


def test_reports_withhold_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-private-token")
    with pytest.raises(ValueError, match="withheld"):
        demo.safe_write(tmp_path / "report.json", {"text": "synthetic-private-token"})
    assert not (tmp_path / "report.json").exists()
    demo.safe_write(tmp_path / "report.json", {"safe": True})
    assert json.loads((tmp_path / "report.json").read_text()) == {"safe": True}


def test_redis_handoff_stops_before_copy_and_restarts_with_bounded_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("AIR_REDIS_CONTAINER", "a" * 64)
    process = Mock(return_value=SimpleNamespace(returncode=0))
    redis = MagicMock()
    redis.__enter__.return_value = redis
    redis.ping.return_value = True
    monkeypatch.setattr(demo.subprocess, "run", process)
    monkeypatch.setattr(demo.Redis, "from_url", Mock(return_value=redis))
    demo.restore_redis(tmp_path)
    assert [call.args[0][1] for call in process.call_args_list] == ["stop", "cp", "start"]
    assert all(call.kwargs["timeout"] == 30 for call in process.call_args_list)
    redis.ping.assert_called_once()


def test_redis_snapshot_checks_disposable_config_before_save(monkeypatch, tmp_path):
    redis = MagicMock()
    redis.__enter__.return_value = redis
    values = {"dir": "/data", "appendonly": "no", "dbfilename": "dump.rdb"}
    redis.config_get.side_effect = lambda key: {key: values[key]}
    monkeypatch.setattr(demo.Redis, "from_url", Mock(return_value=redis))
    command = Mock()
    monkeypatch.setattr(demo, "container_command", command)
    demo.snapshot_redis(tmp_path)
    redis.save.assert_called_once()
    assert command.call_args.args[:2] == ("cp", "{container}:/data/dump.rdb")
    redis.config_get.side_effect = lambda key: {key: "/unexpected"}
    with pytest.raises(ValueError, match="disposable"):
        demo.snapshot_redis(tmp_path)


def test_container_target_and_failures_are_bounded(monkeypatch):
    monkeypatch.setenv("AIR_REDIS_CONTAINER", "unresolved-or-broad-target")
    with pytest.raises(ValueError, match="exact disposable"):
        demo.container_command("stop", "{container}")
    monkeypatch.setenv("AIR_REDIS_CONTAINER", "b" * 64)
    monkeypatch.setattr(demo.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1)))
    with pytest.raises(ValueError, match="operation failed"):
        demo.container_command("stop", "{container}")


def test_credential_separation_before_any_work(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic")
    with pytest.raises(ValueError, match="preparation must not"):
        demo.prepare(tmp_path / "bundle", {})
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ValueError, match="configure"):
        demo.prepare(tmp_path / "bundle", {})
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    with pytest.raises(ValueError, match="without model credentials"):
        demo.publish(tmp_path / "bundle", {})


def test_manual_workflow_scopes_credentials_artifacts_and_human_gate():
    text = (SCRIPT.parents[1] / ".github/workflows/reviewed-live-demo.yml").read_text()
    assert "workflow_dispatch:" in text and "  push:" not in text and "  pull_request:" not in text
    assert "github.run_attempt == 1" in text
    assert "github.event.repository.private == false" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert text.count("${{ secrets.GEMINI_API_KEY }}") == 1
    assert "environment: remediation-demo-review" in text
    assert "needs: prepare" in text
    assert "${{ needs.prepare.outputs.bundle_sha256 }}" in text
    assert text.count("retention-days: 1") == 2
    prepare_text, publish_text = text.split("  publish:")
    assert "contents: write" not in prepare_text
    assert "pull-requests: write" in publish_text
    assert "secrets.GEMINI_API_KEY" not in publish_text
    assert text.count("persist-credentials: false") == 2


@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_both_script_phases_use_actual_adapters_with_mocked_http(monkeypatch, tmp_path, scenario):
    """Exercise script glue too: only external transport, Redis and Docker are replaced."""

    for key in ("GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    fixture = FixtureStructuredModel()
    model_requests = []
    github_requests = []

    def model_handler(request):
        model_requests.append(request)
        payload = json.loads(request.content)
        kind = payload["generationConfig"]["responseJsonSchema"]["title"]
        response = fixture.generate_structured(
            system_prompt=payload["systemInstruction"]["parts"][0]["text"],
            user_prompt=payload["contents"][0]["parts"][0]["text"],
            response_model=RemediationPlan if kind == "RemediationPlan" else ArtifactDraft,
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": response.model_dump_json()}]},
                    }
                ]
            },
        )

    responses = iter(
        [
            httpx.Response(200, json=[]),
            httpx.Response(404),
            httpx.Response(200, json={"object": {"sha": "a" * 40}}),
            httpx.Response(201, json={"ref": "created"}),
            httpx.Response(404),
            httpx.Response(201, json={"commit": {"sha": "b" * 40}}),
            httpx.Response(
                201,
                json={
                    "number": 123,
                    "html_url": "https://github.com/example/demo/pull/123",
                    "head": {"sha": "b" * 40},
                },
            ),
        ]
    )

    def github_handler(request):
        github_requests.append(request)
        return next(responses)

    redis = MagicMock()
    redis.__enter__.return_value = redis
    redis.dbsize.return_value = 0
    monkeypatch.setattr(demo.Redis, "from_url", Mock(return_value=redis))
    saver = InMemorySaver(serde=create_checkpoint_serializer())
    monkeypatch.setattr(demo, "create_redis_checkpointer", lambda _: saver)
    monkeypatch.setattr(demo, "DockerSandboxRunner", FixtureSandboxRunner)
    monkeypatch.setattr(
        demo, "snapshot_redis", lambda path: (path / "dump.rdb").write_bytes(b"RDB")
    )
    restore = Mock()
    monkeypatch.setattr(demo, "restore_redis", restore)
    monkeypatch.setattr(demo, "approved_reviewer", lambda _: "human-reviewer")
    provenance = {
        "repository": "example/demo",
        "sha": "a" * 40,
        "run_id": "12345",
        "scenario": scenario.value,
    }
    bundle = tmp_path / "bundle"
    with (
        httpx.Client(
            base_url="https://generativelanguage.test/",
            transport=httpx.MockTransport(model_handler),
        ) as model_http,
        httpx.Client(
            base_url="https://api.github.test", transport=httpx.MockTransport(github_handler)
        ) as github_http,
    ):
        model = GeminiStructuredModel(
            api_key="synthetic-gemini-key",
            model="gemini-3.8-flash",
            max_output_tokens=1200,
            client=model_http,
        )
        publisher = GitHubPullRequestPublisher(token="synthetic-github-token", client=github_http)
        monkeypatch.setattr(demo.GeminiStructuredModel, "from_settings", lambda _: model)
        monkeypatch.setattr(demo, "GitHubPullRequestPublisher", lambda **_: publisher)
        demo.prepare(bundle, provenance)
        assert len(model_requests) == 2 and not github_requests
        manifest = json.loads((bundle / "manifest.json").read_text())
        monkeypatch.setenv("AIR_BUNDLE_SHA256", demo.bundle_digest(bundle))
        monkeypatch.delenv("GEMINI_API_KEY")
        monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
        demo.publish(bundle, provenance)
    receipt = json.loads((bundle / "publication.json").read_text())
    assert receipt["execution_id"] == manifest["state"]["execution_id"]
    assert manifest["scenario"] == scenario.value
    assert receipt["reviewer"] == "human-reviewer"
    assert receipt["pull_request"]["number"] == 123
    assert len(model_requests) == 2
    assert [r.method for r in github_requests] == [
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
        "PUT",
        "POST",
    ]
    uploaded = json.loads(github_requests[5].content)
    assert b64decode(uploaded["content"]).decode() == (bundle / "proposal.yaml").read_text()
    restore.assert_called_once_with(bundle)
