"""Live application wiring with mocked transport, persistence, sandbox, and publication."""

import importlib
import json
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.llm import GeminiStructuredModel, create_live_model
from agentic_incident_remediation.models import ArtifactDraft, RemediationPlan
from agentic_incident_remediation.operations import FakePullRequestPublisher

app_module = importlib.import_module("agentic_incident_remediation.api.app")


@pytest.mark.parametrize("kind", ["kubernetes_oom_killed", "terraform_drift"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_gemini_live_app_preserves_human_approval_and_closes_clients(
    monkeypatch, kind: str, decision: str
) -> None:
    exercise_live_app(monkeypatch, kind=kind, decision=decision)


def test_gemini_quota_error_fails_closed_without_retry_or_publication(monkeypatch) -> None:
    exercise_live_app(monkeypatch, kind="kubernetes_oom_killed", quota_error=True)


def exercise_live_app(
    monkeypatch,
    *,
    kind: str,
    decision: str = "rejected",
    quota_error: bool = False,
) -> None:
    requests: list[httpx.Request] = []
    fixture = FixtureStructuredModel()
    publisher = FakePullRequestPublisher()
    close_publisher = Mock()
    sandbox = FixtureSandboxRunner()

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if quota_error:
            return httpx.Response(429, json={"error": {"message": "Quota exhausted"}})
        payload = json.loads(request.content)
        schema_title = payload["generationConfig"]["responseJsonSchema"]["title"]
        response_model = RemediationPlan if schema_title == "RemediationPlan" else ArtifactDraft
        response = fixture.generate_structured(
            system_prompt=payload["systemInstruction"]["parts"][0]["text"],
            user_prompt=payload["contents"][0]["parts"][0]["text"],
            response_model=response_model,
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

    settings = Settings(
        _env_file=None,
        model_provider="gemini",
        gemini_api_key="synthetic-gemini-key",
        github_token="synthetic-github-token",
        max_reflection_attempts=1,
    )
    with httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(transport),
    ) as http_client:
        model = create_live_model(settings, client=http_client)
        assert isinstance(model, GeminiStructuredModel)
        close_model = Mock(wraps=model.close)
        monkeypatch.setattr(model, "close", close_model)
        monkeypatch.setattr(app_module, "get_settings", lambda: settings)
        monkeypatch.setattr(app_module, "configure_logging", Mock())
        model_factory = Mock(return_value=model)
        monkeypatch.setattr(app_module, "create_live_model", model_factory)
        monkeypatch.setattr(
            app_module.GitHubPullRequestPublisher,
            "from_settings",
            Mock(return_value=publisher),
        )
        monkeypatch.setattr(publisher, "close", close_publisher, raising=False)
        monkeypatch.setattr(app_module, "create_redis_checkpointer", lambda _: InMemorySaver())
        monkeypatch.setattr(app_module, "DockerSandboxRunner", lambda: sandbox)

        with TestClient(app_module.create_live_app()) as client:
            model_factory.assert_called_once_with(settings)
            started = client.post(
                "/api/incidents",
                json={
                    "id": str(uuid4()),
                    "type": kind,
                    "title": "Synthetic infrastructure incident",
                    "description": "Prepare a declarative correction for review.",
                    "severity": "high",
                    "source": "mocked-live-runtime-test",
                },
            )
            assert started.status_code == 202
            state = started.json()
            assert not publisher.proposals
            if quota_error:
                assert state["status"] == "failed"
                assert state["review_ready"] is False
                assert state["approval"] is None
                assert not state["artifacts"]
                assert not sandbox.requests
                assert len(requests) == 1
            else:
                assert state["status"] == "awaiting_approval"
                assert state["review_ready"] is True
                assert state["validation_results"][-1]["status"] == "passed"
                assert len(requests) == 2
                result = client.post(
                    f"/api/executions/{state['execution_id']}/approval",
                    json={
                        "approval_id": state["approval"]["id"],
                        "decision": decision,
                        "decided_by": "synthetic-reviewer",
                        "comment": "Reviewed mocked validation output.",
                    },
                )
                assert result.status_code == 200
                expected = "completed" if decision == "approved" else "rejected"
                assert result.json()["status"] == expected
                assert len(publisher.proposals) == (1 if decision == "approved" else 0)
                assert len(requests) == 2  # Human decisions do not call the model again.

        close_model.assert_called_once_with()
        close_publisher.assert_called_once_with()
        assert not http_client.is_closed  # The gateway does not own an injected client.
