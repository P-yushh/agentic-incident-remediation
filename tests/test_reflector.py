"""Bounded reflection and self-correcting workflow tests."""

import json
from typing import Any

import pytest

from agentic_incident_remediation.artifacts import materialize_artifact
from agentic_incident_remediation.graph import (
    ReflectorRoute,
    SelfCorrectingRemediationWorkflow,
    build_reflection_graph,
    route_after_reflector,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    AgentState,
    ArtifactDraft,
    Incident,
    IncidentType,
    RemediationPlan,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.reflector import ReflectorNode
from agentic_incident_remediation.sandbox import (
    FakeSandboxRunner,
    SandboxExecution,
    ValidationTool,
    normalize_validation_result,
)


def make_incident() -> Incident:
    return Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Synthetic memory incident",
        description="The payment API exceeded its configured memory limit.",
        source="test-fixture",
    )


def make_plan_payload(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Generate, validate, and review a safe memory-limit correction.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate a proposal",
                "description": "Create one declarative Kubernetes artifact.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate in isolation",
                "description": "Run an allowlisted validator in a locked-down container.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Request review",
                "description": "Require human approval before repository operations.",
            },
        ],
    }


def artifact_payload(memory: str) -> dict[str, str]:
    return {
        "kind": "kubernetes_manifest",
        "path": "remediations/payment-api.yaml",
        "content": f"""apiVersion: apps/v1
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
              memory: {memory}
""",
        "change_summary": f"Set the payment API memory limit to {memory}.",
    }


def kube_execution(*, invalid: bool) -> SandboxExecution:
    resources = []
    if invalid:
        resources = [
            {
                "filename": "stdin",
                "kind": "Deployment",
                "version": "apps/v1",
                "status": "INVALID",
                "msg": "Additional property resourcez is not allowed",
            }
        ]
    return SandboxExecution(
        tool=ValidationTool.KUBECONFORM,
        exit_code=1 if invalid else 0,
        stdout=json.dumps(
            {
                "resources": resources,
                "summary": {
                    "valid": 0 if invalid else 1,
                    "invalid": 1 if invalid else 0,
                },
            }
        ),
        duration_ms=8,
    )


def reflecting_state(*, retry_count: int = 0, max_retries: int = 2) -> AgentState:
    incident = make_incident()
    artifact = materialize_artifact(ArtifactDraft.model_validate(artifact_payload("512Mi")))
    result = normalize_validation_result(artifact.id, kube_execution(invalid=True))
    return AgentState(
        incident=incident,
        status=WorkflowStatus.REFLECTING,
        plan=RemediationPlan.model_validate(make_plan_payload(incident)),
        artifacts=(artifact,),
        validation_results=(result,),
        validation_errors=(result.findings[0].message,),
        retry_count=retry_count,
        max_retries=max_retries,
    )


def test_reflector_schedules_a_bounded_revision_with_validation_feedback() -> None:
    state = reflecting_state()

    result = AgentState.model_validate(state.model_dump() | ReflectorNode()(state))

    assert result.status is WorkflowStatus.GENERATING
    assert result.retry_count == 1
    assert result.failure_reason is None
    assert result.messages[-1].name == "reflector"
    assert "Retry 1/2" in result.messages[-1].content
    assert "resourcez" in result.messages[-1].content
    assert route_after_reflector(result) is ReflectorRoute.GENERATION_READY


def test_reflector_fails_safely_when_retry_ceiling_is_exhausted() -> None:
    state = reflecting_state(retry_count=1, max_retries=1)

    result = AgentState.model_validate(state.model_dump() | ReflectorNode()(state))

    assert result.status is WorkflowStatus.FAILED
    assert result.retry_count == 1
    assert result.failure_reason is not None
    assert "retry limit of 1 exhausted" in result.failure_reason
    assert result.messages[-1].name == "reflector"
    assert route_after_reflector(result) is ReflectorRoute.FAILED


def test_reflector_rejects_unusable_workflow_states() -> None:
    incident = make_incident()
    artifact = materialize_artifact(ArtifactDraft.model_validate(artifact_payload("512Mi")))
    passed = normalize_validation_result(artifact.id, kube_execution(invalid=False))

    with pytest.raises(ValueError, match="reflecting status"):
        ReflectorNode()(AgentState(incident=incident))
    with pytest.raises(ValueError, match="artifact and validation errors"):
        ReflectorNode()(AgentState(incident=incident, status=WorkflowStatus.REFLECTING))
    with pytest.raises(ValueError, match="failed result"):
        ReflectorNode()(
            AgentState(
                incident=incident,
                status=WorkflowStatus.REFLECTING,
                artifacts=(artifact,),
                validation_results=(passed,),
                validation_errors=("stale failure",),
            )
        )


def test_self_correcting_workflow_revises_failed_artifact_until_it_passes() -> None:
    incident = make_incident()
    model = FakeStructuredModel(
        [
            make_plan_payload(incident),
            artifact_payload("512Mi"),
            artifact_payload("640Mi"),
        ]
    )
    runner = FakeSandboxRunner([kube_execution(invalid=True), kube_execution(invalid=False)])

    result = SelfCorrectingRemediationWorkflow(model, runner, max_retries=2).invoke(incident)

    assert result.status is WorkflowStatus.AWAITING_APPROVAL
    assert result.retry_count == 1
    assert len(result.artifacts) == 2
    assert result.artifacts[-1].revision == 2
    assert result.artifacts[-1].parent_id == result.artifacts[0].id
    assert [item.status for item in result.validation_results] == [
        ValidationStatus.FAILED,
        ValidationStatus.PASSED,
    ]
    assert [item.artifact_id for item in result.validation_results] == [
        result.artifacts[0].id,
        result.artifacts[1].id,
    ]
    assert "resourcez" in model.requests[2].user_prompt
    assert result.artifacts[0].content in model.requests[2].user_prompt
    assert len(model.requests) == 3


def test_self_correcting_workflow_stops_after_configured_retry_limit() -> None:
    incident = make_incident()
    model = FakeStructuredModel(
        [
            make_plan_payload(incident),
            artifact_payload("512Mi"),
            artifact_payload("640Mi"),
            artifact_payload("768Mi"),
        ]
    )
    runner = FakeSandboxRunner([kube_execution(invalid=True)] * 3)

    result = SelfCorrectingRemediationWorkflow(model, runner, max_retries=2).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.retry_count == 2
    assert len(result.artifacts) == 3
    assert len(result.validation_results) == 3
    assert result.failure_reason is not None
    assert "retry limit of 2 exhausted" in result.failure_reason
    assert len(model.requests) == 4
    assert len(runner.requests) == 3


def test_reflection_graph_exposes_validation_feedback_loop() -> None:
    graph = build_reflection_graph(FakeStructuredModel([]), FakeSandboxRunner([]))
    drawable = graph.get_graph()

    assert {"validator", "reflector", "generator"}.issubset(drawable.nodes)
    assert any(edge.source == "validator" and edge.target == "reflector" for edge in drawable.edges)
    assert any(edge.source == "reflector" and edge.target == "generator" for edge in drawable.edges)
    with pytest.raises(ValueError, match="unroutable"):
        route_after_reflector(AgentState(incident=make_incident()))
