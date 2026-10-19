"""Experimental mode uses local inference and typed remote validation; no real API writes."""

import json
from contextlib import nullcontext
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from agentic_incident_remediation.actions import local_generation
from agentic_incident_remediation.actions.models import StartRun
from agentic_incident_remediation.demo import FixtureStructuredModel
from agentic_incident_remediation.generator.node import GeneratorNode
from agentic_incident_remediation.llm.local_handoff import LocalProposal, parse_local_proposal
from agentic_incident_remediation.models import AgentState, ArtifactDraft, Incident, WorkflowStatus
from agentic_incident_remediation.planner.node import PlannerNode
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario


def proposal(scenario=DemoScenario.OOM):
    state = AgentState(
        incident=Incident.model_validate(SCENARIOS[scenario].incident()),
        status=WorkflowStatus.PLANNING,
    )
    model = FixtureStructuredModel()
    state = state.model_copy(update=PlannerNode(model)(state))
    state = state.model_copy(update=GeneratorNode(model)(state))
    return LocalProposal(
        scenario=scenario,
        incident_id=state.incident.id,
        plan=state.plan,
        artifact=ArtifactDraft.model_validate(
            state.artifacts[0].model_dump(include={"kind", "path", "content", "change_summary"})
        ),
    )


@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_proposal_round_trip_and_real_nodes_with_expanded_budget(monkeypatch, scenario):
    factory = Mock(return_value=nullcontext(FixtureStructuredModel()))
    monkeypatch.setattr(local_generation, "OllamaStructuredModel", factory)
    result = local_generation.generate_proposal(scenario, output_tokens=8192, timeout_seconds=600)
    factory.assert_called_once_with(
        max_output_tokens=8192, timeout_seconds=600, context_tokens=16384
    )
    assert parse_local_proposal(result.wire(), scenario) == result
    assert result.plan.incident_id == result.incident_id


def test_failed_local_output_does_not_get_repaired_or_fall_back(monkeypatch):
    from agentic_incident_remediation.llm import FakeStructuredModel, ModelOutputError

    monkeypatch.setattr(
        local_generation,
        "OllamaStructuredModel",
        lambda **kw: nullcontext(FakeStructuredModel([ModelOutputError("incomplete")])),
    )
    with pytest.raises(ValueError, match="incomplete"):
        local_generation.generate_proposal(
            DemoScenario.OOM, output_tokens=8192, timeout_seconds=600
        )


@pytest.mark.parametrize("stops", [[True], [False, True]])
def test_cancellation_does_not_start_another_model_call(monkeypatch, stops):
    fixture = FixtureStructuredModel()
    monkeypatch.setattr(
        local_generation, "OllamaStructuredModel", lambda **kw: nullcontext(fixture)
    )
    checks = iter(stops)
    with pytest.raises(ValueError, match="cancelled"):
        local_generation.generate_proposal(
            DemoScenario.OOM,
            output_tokens=8192,
            timeout_seconds=600,
            should_stop=lambda: next(checks),
        )
    assert len(fixture.requests) == len(stops) - 1


def test_handoff_rejects_changed_identity_case_kind_or_oversize():
    result = proposal()
    with pytest.raises(ValueError):
        parse_local_proposal(result.wire(), DemoScenario.TERRAFORM)
    data = result.model_dump(mode="json")
    data["incident_id"] = str(uuid4())
    with pytest.raises(ValueError):
        LocalProposal.model_validate(data)
    data = result.model_dump(mode="json")
    data["artifact"] = proposal(DemoScenario.TERRAFORM).artifact.model_dump(mode="json")
    with pytest.raises(ValueError):
        LocalProposal.model_validate(data)
    with pytest.raises(ValueError):
        parse_local_proposal("x" * 55001, DemoScenario.OOM)
    large = result.model_copy(
        update={"artifact": result.artifact.model_copy(update={"content": "x" * 56000})}
    )
    with pytest.raises(ValueError, match="size"):
        large.wire()


@pytest.mark.parametrize("cloud", [False, True])
def test_readiness_contacts_loopback_only_and_never_generates(monkeypatch, cloud):
    calls = []
    original_client = httpx.Client

    def handler(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        assert request.url.path == "/api/show"
        return httpx.Response(200, json={"remote_host": "cloud"} if cloud else {})

    monkeypatch.setattr(
        local_generation.httpx,
        "Client",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    if cloud:
        with pytest.raises(ValueError, match="no cloud fallback"):
            local_generation.local_readiness()
    else:
        local_generation.local_readiness()
    assert len(calls) == 2


@pytest.fixture
def deferred(service, monkeypatch):
    service.experimental_ollama = True
    submit = Mock()
    monkeypatch.setattr(service._executor, "submit", submit)
    monkeypatch.setattr(
        "agentic_incident_remediation.actions.service.local_readiness", lambda: None
    )
    monkeypatch.setattr(
        "agentic_incident_remediation.actions.service.generate_proposal",
        lambda *a, **kw: proposal(),
    )
    return submit


def test_disabled_by_default_and_invalid_settings(browser):
    body = {
        "request_id": str(uuid4()),
        "confirm_live_calls": True,
        "generation_mode": "ollama_local",
    }
    assert browser.get("/api/actions/generation-modes").json()["experimental_ollama"] is False
    assert browser.post("/api/actions/runs", json=body).status_code == 409
    for extra in [
        {"generation_mode": "unknown"},
        {"ollama_output_tokens": 8193},
        {"ollama_timeout_seconds": 0},
    ]:
        assert browser.post("/api/actions/runs", json=body | extra).status_code == 422


def test_local_start_is_deferred_idempotent_and_binds_settings(service, deferred, api):
    identifier = uuid4()
    record = service.start_local(identifier, DemoScenario.OOM, 8192, 600)
    assert record.stage == "local_generating" and record.review is None
    assert service.start_local(identifier, DemoScenario.OOM, 8192, 600).id == identifier
    assert deferred.call_count == 1
    assert not any(r.method == "POST" for r in api.requests)
    for args in [(DemoScenario.OOM, 4096, 600), (DemoScenario.CRASH_LOOP, 8192, 600)]:
        with pytest.raises(ValueError):
            service.start_local(identifier, *args)
    with pytest.raises(ValueError):
        service.start(identifier)
    worker, intended = deferred.call_args.args
    worker(intended)
    saved = service.store.get(identifier)
    assert saved.stage == "queued" and saved.run_id == api.run_id
    request = next(r for r in api.requests if r.url.path.endswith("/dispatches"))
    inputs = json.loads(request.content)["inputs"]
    assert inputs["generation_mode"] == "ollama_local"
    assert (
        parse_local_proposal(inputs["local_proposal"], DemoScenario.OOM).artifact.kind
        == "kubernetes_manifest"
    )


def test_http_submission_returns_accepted_before_inference(browser, service, deferred):
    identifier = str(uuid4())
    response = browser.post(
        "/api/actions/runs",
        json={
            "request_id": identifier,
            "confirm_live_calls": True,
            "generation_mode": "ollama_local",
            "scenario": "kubernetes_oom_killed",
            "ollama_output_tokens": 4096,
            "ollama_timeout_seconds": 300,
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["id"] == identifier and body["stage"] == "local_generating"
    assert body["run_id"] is None and body["review"] is None
    assert body["ollama_output_tokens"] == 4096
    assert browser.get("/api/actions/runs/" + identifier).json()["stage"] == "local_generating"
    assert deferred.call_count == 1


def test_cancelled_local_generation_cannot_dispatch(service, deferred, api):
    record = service.start_local(uuid4(), DemoScenario.OOM, 8192, 600)
    cancelled = service.cancel(record.id)
    assert cancelled.stage == "cancelled"
    worker, intended = deferred.call_args.args
    worker(intended)
    assert service.store.get(record.id).stage == "cancelled"
    assert not any(r.method == "POST" for r in api.requests)


def test_ambiguous_handoff_is_not_retried(service, deferred, api):
    record = service.start_local(uuid4(), DemoScenario.OOM, 8192, 600)
    api.dispatch_error = True
    worker, intended = deferred.call_args.args
    worker(intended)
    assert service.store.get(record.id).stage == "dispatch_unknown"
    service.start_local(record.id, DemoScenario.OOM, 8192, 600)
    assert deferred.call_count == 1
    assert sum(r.method == "POST" for r in api.requests) == 1


def test_generation_error_is_local_not_a_github_job_failure(service, deferred, monkeypatch):
    monkeypatch.setattr(
        "agentic_incident_remediation.actions.service.generate_proposal",
        Mock(side_effect=RuntimeError("sensitive")),
    )
    record = service.start_local(uuid4(), DemoScenario.OOM, 8192, 600)
    worker, intended = deferred.call_args.args
    worker(intended)
    result = service.store.get(record.id)
    assert result.stage == "failed" and result.failure_origin == "local"
    assert "sensitive" not in result.model_dump_json()
    assert result.review is None and result.run_id is None


def test_unpublished_handoff_blocks_before_generation(service, deferred, monkeypatch):
    from agentic_incident_remediation.actions.github import ActionsError

    monkeypatch.setattr(service.github, "readiness", lambda: {"ollama_supported": False})
    with pytest.raises(ActionsError, match="Publish"):
        service.start_local(uuid4(), DemoScenario.OOM, 8192, 600)
    assert deferred.call_count == 0 and not service.store.list()


def test_strict_opt_in_request_still_requires_consent():
    with pytest.raises(ValueError):
        StartRun(request_id=uuid4(), confirm_live_calls=False, generation_mode="ollama_local")
