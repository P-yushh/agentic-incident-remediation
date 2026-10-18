"""Fresh incidents and approval continuity without provider calls or repository writes."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import FakeStructuredModel, ModelGatewayError
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.models import AgentState
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.persistence.checkpoints import create_checkpoint_serializer
from agentic_incident_remediation.review_demo import (
    PublicationDisabled,
    prepare_review,
    publish_review,
    require_pending_review,
)
from agentic_incident_remediation.review_scenarios import DemoScenario
from agentic_incident_remediation.sandbox import FakeSandboxRunner, SandboxExecution, ValidationTool


def workflow(model, runner, publisher, saver):
    return DurableApprovalWorkflow(
        model, runner, publisher, repository="example/synthetic", checkpointer=saver, max_retries=1
    )


def pending_review(scenario=DemoScenario.OOM):
    saver = InMemorySaver(serde=create_checkpoint_serializer())
    model = FixtureStructuredModel()
    budget = BudgetedModel(model)
    runner = FixtureSandboxRunner()
    first = workflow(budget, runner, PublicationDisabled(), saver)
    manifest = prepare_review(first, budget, {"repository": "example/synthetic"}, scenario)
    return saver, manifest, model, runner


@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_fresh_incident_restores_exact_interrupt_then_publishes_once(scenario) -> None:
    saver, manifest, model, runner = pending_review(scenario)
    publisher = FakePullRequestPublisher()
    no_calls = FakeStructuredModel([])
    no_validation = FakeSandboxRunner([])
    restored = workflow(no_calls, no_validation, publisher, saver)
    execution_id = UUID(manifest["state"]["execution_id"])
    assert restored.ready_for_review(execution_id)
    assert not publisher.proposals

    result = publish_review(restored, manifest, reviewer="human-reviewer")

    assert result["status"] == "completed"
    assert manifest["scenario"] == scenario.value
    assert result["execution_id"] == manifest["state"]["execution_id"]
    assert result["approval_id"] == manifest["state"]["approval"]["id"]
    assert result["artifact_sha256"] == manifest["artifact_sha256"]
    assert result["publication_model_calls"] == 0
    assert result["merge_performed"] is False and result["deployment_performed"] is False
    assert len(model.requests) == 2 and len(runner.requests) == 1
    assert not no_calls.requests and not no_validation.requests
    assert len(publisher.proposals) == 1
    assert publisher.proposals[0].artifact.content == manifest["state"]["artifacts"][0]["content"]

    with pytest.raises(ValueError, match="differs"):
        publish_review(restored, manifest, reviewer="human-reviewer")
    assert len(publisher.proposals) == 1


def test_stale_approval_and_rejection_never_publish() -> None:
    saver, manifest, _, _ = pending_review()
    publisher = FakePullRequestPublisher()
    restored = workflow(FakeStructuredModel([]), FakeSandboxRunner([]), publisher, saver)
    base = f"/api/executions/{manifest['state']['execution_id']}/approval"
    decision = {"approval_id": str(uuid4()), "decision": "rejected", "decided_by": "human"}
    with TestClient(create_app(RemediationService(restored))) as client:
        assert client.post(base, json=decision).status_code == 409
        decision["approval_id"] = manifest["state"]["approval"]["id"]
        assert client.post(base, json=decision).json()["status"] == "rejected"
        assert client.post(base, json=decision).status_code == 409
    assert not publisher.proposals


@pytest.mark.parametrize("change", ["state", "checksum", "version", "synthetic"])
def test_changed_review_is_rejected_before_publication(change) -> None:
    saver, original, _, _ = pending_review()
    manifest = deepcopy(original)
    if change == "state":
        manifest["state"]["incident"]["title"] = "Changed after review"
    elif change == "checksum":
        manifest["artifact_sha256"] = "0" * 64
    elif change == "version":
        manifest["version"] = 999
    else:
        manifest["synthetic"] = False
    publisher = FakePullRequestPublisher()
    restored = workflow(FakeStructuredModel([]), FakeSandboxRunner([]), publisher, saver)
    with pytest.raises(ValueError):
        publish_review(restored, manifest, reviewer="human")
    assert not publisher.proposals


def test_quota_and_validation_failure_never_produce_a_review_bundle() -> None:
    for model, runner in (
        (FakeStructuredModel([ModelGatewayError("quota")]), FixtureSandboxRunner()),
        (
            FixtureStructuredModel(),
            FakeSandboxRunner(
                [SandboxExecution(tool=ValidationTool.KUBECONFORM, exit_code=1, duration_ms=1)]
            ),
        ),
    ):
        budget = BudgetedModel(model)
        first = workflow(budget, runner, PublicationDisabled(), InMemorySaver())
        with pytest.raises(ValueError, match="pending native review"):
            prepare_review(first, budget, {})
        assert budget.calls <= 2


def test_review_checks_require_validation_history_and_correct_budget(monkeypatch) -> None:
    saver, manifest, _, _ = pending_review()
    restored = workflow(
        FakeStructuredModel([]), FakeSandboxRunner([]), PublicationDisabled(), saver
    )
    pending = AgentState.model_validate(manifest["state"])
    with pytest.raises(ValueError, match="validation"):
        require_pending_review(restored, pending.model_copy(update={"validation_results": ()}))
    monkeypatch.setattr(restored, "get_history", lambda _: ())
    with pytest.raises(ValueError, match="history"):
        require_pending_review(restored, pending)

    budget = BudgetedModel(FixtureStructuredModel())
    first = workflow(budget, FixtureSandboxRunner(), PublicationDisabled(), InMemorySaver())
    unrelated_budget = BudgetedModel(FixtureStructuredModel())
    with pytest.raises(ValueError, match="exactly one"):
        prepare_review(first, unrelated_budget, {})


def test_publication_failure_does_not_claim_success() -> None:
    saver, manifest, _, _ = pending_review()
    restored = workflow(
        FakeStructuredModel([]), FakeSandboxRunner([]), PublicationDisabled(), saver
    )
    with pytest.raises(ValueError, match="did not complete"):
        publish_review(restored, manifest, reviewer="human")


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BudgetedModel(FixtureStructuredModel(), maximum=0)
