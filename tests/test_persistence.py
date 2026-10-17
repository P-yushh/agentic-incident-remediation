"""Durable checkpoint and workflow recovery tests."""

import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.demo import FixtureStructuredModel
from agentic_incident_remediation.graph import (
    DurableApprovalWorkflow,
    SelfCorrectingRemediationWorkflow,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    ApprovalResponse,
    Incident,
    IncidentType,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import StrictModel
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.persistence import checkpoint_config, create_redis_checkpointer
from agentic_incident_remediation.persistence.checkpoints import create_checkpoint_serializer
from agentic_incident_remediation.sandbox import (
    FakeSandboxRunner,
    SandboxExecution,
    ValidationTool,
)


def make_incident() -> Incident:
    return Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Synthetic durable execution",
        description="The payment API exceeded its memory limit.",
        source="test-fixture",
    )


def make_plan(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Generate and validate a durable remediation proposal.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate",
                "description": "Generate one Kubernetes proposal.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate",
                "description": "Validate the proposal in isolation.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Review",
                "description": "Request human approval.",
            },
        ],
    }


def artifact_payload() -> dict[str, str]:
    return {
        "kind": "kubernetes_manifest",
        "path": "remediations/payment-api.yaml",
        "content": """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-api
spec:
  selector:
    matchLabels:
      app: payment-api
  template:
    metadata:
      labels:
        app: payment-api
    spec:
      containers:
        - name: api
          image: example/payment-api:1.0
          resources:
            limits:
              memory: 640Mi
""",
        "change_summary": "Increase the payment API memory limit.",
    }


def passing_execution() -> SandboxExecution:
    return SandboxExecution(
        tool=ValidationTool.KUBECONFORM,
        exit_code=0,
        stdout=json.dumps({"resources": [], "summary": {"valid": 1, "invalid": 0}}),
        duration_ms=7,
    )


def test_checkpoint_config_uses_execution_id_as_stable_thread_identity() -> None:
    execution_id = uuid4()

    config = checkpoint_config(execution_id, recursion_limit=22)

    assert config["configurable"] == {"thread_id": str(execution_id)}
    assert config["recursion_limit"] == 22


def test_workflow_state_and_history_survive_new_workflow_instance() -> None:
    incident = make_incident()
    execution_id = uuid4()
    saver = InMemorySaver()
    model = FakeStructuredModel([make_plan(incident), artifact_payload()])
    original = SelfCorrectingRemediationWorkflow(
        model,
        FakeSandboxRunner([passing_execution()]),
        checkpointer=saver,
    )

    completed = original.invoke(incident, execution_id=execution_id)
    restarted = SelfCorrectingRemediationWorkflow(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
        checkpointer=saver,
    )
    recovered = restarted.get_state(execution_id)
    history = restarted.get_history(execution_id)

    assert completed.status is WorkflowStatus.AWAITING_APPROVAL
    assert recovered == completed
    assert history[0] == completed
    assert len(history) >= 5
    assert any(snapshot.status is WorkflowStatus.PLANNING for snapshot in history)
    assert any(snapshot.status is WorkflowStatus.VALIDATING for snapshot in history)


def test_recovery_requires_a_checkpointer_and_known_execution() -> None:
    workflow = SelfCorrectingRemediationWorkflow(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
    )

    with pytest.raises(RuntimeError, match="configured checkpointer"):
        workflow.get_state(uuid4())

    durable = SelfCorrectingRemediationWorkflow(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
        checkpointer=InMemorySaver(),
    )
    with pytest.raises(LookupError, match="was not found"):
        durable.get_state(uuid4())


def test_redis_checkpointer_is_initialized_before_use(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class StubRedisSaver:
        def __init__(self, *, redis_url: str) -> None:
            events.append(("created", redis_url))

        def setup(self) -> None:
            events.append("setup")

    monkeypatch.setattr(
        "agentic_incident_remediation.persistence.checkpoints.RedisSaver",
        StubRedisSaver,
    )

    saver = create_redis_checkpointer("redis://localhost:6379/0")

    assert isinstance(saver, StubRedisSaver)
    assert events == [("created", "redis://localhost:6379/0"), "setup"]
    with pytest.raises(ValueError, match="cannot be empty"):
        create_redis_checkpointer("  ")


def test_redis_json_serialization_restores_typed_state_and_native_approval() -> None:
    serializer = create_checkpoint_serializer()
    saver = InMemorySaver(serde=serializer)
    publisher = FakePullRequestPublisher()
    original = DurableApprovalWorkflow(
        FixtureStructuredModel(),
        FakeSandboxRunner([passing_execution()]),
        publisher,
        repository="example/synthetic-integration",
        checkpointer=saver,
    )
    pending = original.start(make_incident())
    restarted = DurableApprovalWorkflow(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
        publisher,
        repository="example/synthetic-integration",
        checkpointer=saver,
    )

    recovered = restarted.get_state(pending.execution_id)

    assert recovered == pending
    assert restarted.ready_for_review(pending.execution_id)
    assert recovered.approval is not None
    rejected = restarted.resume(
        pending.execution_id,
        ApprovalResponse(
            approval_id=recovered.approval.id,
            decision="rejected",
            decided_by="serializer-test-reviewer",
        ),
    )
    assert rejected.status is WorkflowStatus.REJECTED
    assert publisher.proposals == []


def test_checkpoint_serializer_does_not_revive_unknown_models_or_enable_pickle() -> None:
    class UntrustedContract(StrictModel):
        value: str

    serializer = create_checkpoint_serializer()
    encoded = serializer.dumps_typed(UntrustedContract(value="untrusted"))

    assert not isinstance(serializer.loads_typed(encoded), UntrustedContract)
    assert serializer.pickle_fallback is False
    with pytest.raises(NotImplementedError):
        serializer.loads_typed(("pickle", b"not-a-pickle"))


def test_approval_execution_persists_before_advancing_at_every_entry_point() -> None:
    workflow = DurableApprovalWorkflow(
        FixtureStructuredModel(),
        FakeSandboxRunner([passing_execution(), passing_execution()]),
        FakePullRequestPublisher(),
        repository="example/synthetic-integration",
        checkpointer=InMemorySaver(),
    )
    with patch.object(workflow._graph, "invoke", wraps=workflow._graph.invoke) as invoke:
        pending = workflow.start(make_incident())
        assert pending.approval is not None
        workflow.resume(
            pending.execution_id,
            ApprovalResponse(
                approval_id=pending.approval.id,
                decision="rejected",
                decided_by="durability-test-reviewer",
            ),
        )
        prepared = workflow.prepare(make_incident())
        for _ in range(10):
            workflow.advance(prepared.execution_id)
            if workflow.ready_for_review(prepared.execution_id):
                break
        assert workflow.ready_for_review(prepared.execution_id)

    assert invoke.call_count > 3  # start, resume, prepare, and node-by-node advance
    assert all(call.kwargs.get("durability") == "sync" for call in invoke.call_args_list)
