"""Durable human approval and post-approval operation tests."""

import json
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agentic_incident_remediation.graph import (
    ApprovalRoute,
    DurableApprovalWorkflow,
    build_approval_graph,
    route_after_approval,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    AgentState,
    ApprovalDecision,
    ApprovalResponse,
    Incident,
    IncidentType,
    PullRequestRecord,
    WorkflowStatus,
)
from agentic_incident_remediation.operations import (
    FakePullRequestPublisher,
    OperationsError,
)
from agentic_incident_remediation.sandbox import (
    FakeSandboxRunner,
    SandboxExecution,
    ValidationTool,
)

REPOSITORY = "P-yushh/agentic-incident-remediation"


def make_incident() -> Incident:
    return Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Payment API pod ran out of memory",
        description="The container exceeded its configured memory limit.",
        source="test-fixture",
    )


def make_plan(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Create a validated proposal and request human approval.",
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
                "description": "Pause for a human decision.",
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
        duration_ms=6,
    )


def start_workflow(
    *,
    saver: InMemorySaver | None = None,
    publisher: FakePullRequestPublisher | None = None,
) -> tuple[DurableApprovalWorkflow, AgentState, InMemorySaver, FakePullRequestPublisher]:
    incident = make_incident()
    checkpointer = saver or InMemorySaver()
    operations = publisher or FakePullRequestPublisher()
    workflow = DurableApprovalWorkflow(
        FakeStructuredModel([make_plan(incident), artifact_payload()]),
        FakeSandboxRunner([passing_execution()]),
        operations,
        repository=REPOSITORY,
        checkpointer=checkpointer,
    )
    state = workflow.start(incident)
    return workflow, state, checkpointer, operations


def test_workflow_pauses_with_stable_pending_approval() -> None:
    workflow, state, _, publisher = start_workflow()

    restored = workflow.get_state(state.execution_id)

    assert state.status is WorkflowStatus.AWAITING_APPROVAL
    assert state.approval is not None
    assert state.approval.decision is ApprovalDecision.PENDING
    assert state.approval.artifact_ids == (state.artifacts[-1].id,)
    assert restored == state
    assert publisher.proposals == []
    assert state.messages[-1].name == "approval-gate"


def test_approved_execution_resumes_after_restart_and_creates_only_a_pr() -> None:
    _, pending, saver, publisher = start_workflow()
    assert pending.approval is not None
    restarted = DurableApprovalWorkflow(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
        publisher,
        repository=REPOSITORY,
        checkpointer=saver,
    )
    response = ApprovalResponse(
        approval_id=pending.approval.id,
        decision=ApprovalDecision.APPROVED,
        decided_by="reviewer@example.com",
        comment="Validated and safe to propose.",
    )

    completed = restarted.resume(pending.execution_id, response)

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.approval is not None
    assert completed.approval.decision is ApprovalDecision.APPROVED
    assert completed.pull_request is not None
    assert completed.pull_request.url.endswith("/pull/42")
    assert len(publisher.proposals) == 1
    proposal = publisher.proposals[0]
    assert proposal.approval_id == response.approval_id
    assert proposal.artifact == completed.artifacts[-1]
    assert proposal.branch_name.startswith("remediation/kubernetes-oom-killed-")
    assert "No Kubernetes or Terraform apply command was executed" in proposal.body
    assert "kubectl apply" not in proposal.artifact.content.lower()


def test_rejection_finishes_without_repository_side_effect() -> None:
    workflow, pending, _, publisher = start_workflow()
    assert pending.approval is not None

    rejected = workflow.resume(
        pending.execution_id,
        ApprovalResponse(
            approval_id=pending.approval.id,
            decision=ApprovalDecision.REJECTED,
            decided_by="reviewer@example.com",
            comment="Increase requires capacity review.",
        ),
    )

    assert rejected.status is WorkflowStatus.REJECTED
    assert rejected.pull_request is None
    assert publisher.proposals == []
    assert route_after_approval(rejected) is ApprovalRoute.REJECTED


def test_mismatched_or_pending_decision_cannot_resume_execution() -> None:
    workflow, pending, _, publisher = start_workflow()
    assert pending.approval is not None

    with pytest.raises(ValueError, match="does not match"):
        workflow.resume(
            pending.execution_id,
            ApprovalResponse(
                approval_id=uuid4(),
                decision=ApprovalDecision.APPROVED,
                decided_by="reviewer@example.com",
            ),
        )
    with pytest.raises(ValidationError, match="must approve or reject"):
        ApprovalResponse(
            approval_id=pending.approval.id,
            decision=ApprovalDecision.PENDING,
            decided_by="reviewer@example.com",
        )

    assert workflow.get_state(pending.execution_id).status is WorkflowStatus.AWAITING_APPROVAL
    assert publisher.proposals == []


def test_repository_failure_is_terminal_and_does_not_claim_a_pull_request() -> None:
    publisher = FakePullRequestPublisher([OperationsError("GitHub unavailable")])
    workflow, pending, _, _ = start_workflow(publisher=publisher)
    assert pending.approval is not None

    failed = workflow.resume(
        pending.execution_id,
        ApprovalResponse(
            approval_id=pending.approval.id,
            decision=ApprovalDecision.APPROVED,
            decided_by="reviewer@example.com",
        ),
    )

    assert failed.status is WorkflowStatus.FAILED
    assert failed.pull_request is None
    assert failed.failure_reason == "approved pull request publication failed safely"


def test_executor_rejects_pull_request_record_for_another_repository() -> None:
    publisher = FakePullRequestPublisher(
        [
            PullRequestRecord(
                repository="another-owner/another-repository",
                number=3,
                url="https://github.com/another-owner/another-repository/pull/3",
                branch_name="remediation/unrelated",
                commit_sha="d" * 40,
            )
        ]
    )
    workflow, pending, _, _ = start_workflow(publisher=publisher)
    assert pending.approval is not None

    failed = workflow.resume(
        pending.execution_id,
        ApprovalResponse(
            approval_id=pending.approval.id,
            decision=ApprovalDecision.APPROVED,
            decided_by="reviewer@example.com",
        ),
    )

    assert failed.status is WorkflowStatus.FAILED
    assert failed.pull_request is None


def test_approval_graph_requires_durable_checkpointer_and_exposes_gate() -> None:
    with pytest.raises(ValueError, match="requires a checkpointer"):
        build_approval_graph(
            FakeStructuredModel([]),
            FakeSandboxRunner([]),
            FakePullRequestPublisher(),
            repository=REPOSITORY,
            checkpointer=None,
        )

    graph = build_approval_graph(
        FakeStructuredModel([]),
        FakeSandboxRunner([]),
        FakePullRequestPublisher(),
        repository=REPOSITORY,
        checkpointer=InMemorySaver(),
    ).get_graph()

    assert {"prepare_approval", "approval_gate", "pull_request_executor"}.issubset(graph.nodes)
    assert any(
        edge.source == "prepare_approval" and edge.target == "approval_gate" for edge in graph.edges
    )
    with pytest.raises(ValueError, match="unroutable"):
        route_after_approval(AgentState(incident=make_incident()))
