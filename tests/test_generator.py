"""Generator node and end-to-end planning-to-artifact tests."""

from typing import Any

import pytest

from agentic_incident_remediation.artifacts import materialize_artifact
from agentic_incident_remediation.generator import (
    GeneratorNode,
    artifact_kind_for_incident,
    build_generator_prompts,
)
from agentic_incident_remediation.graph import (
    GeneratorRoute,
    RemediationWorkflow,
    build_generation_graph,
    route_after_generator,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    AgentState,
    ArtifactDraft,
    ArtifactKind,
    Incident,
    IncidentType,
    RemediationPlan,
    WorkflowStatus,
)


def make_incident(incident_type: IncidentType) -> Incident:
    return Incident(
        type=incident_type,
        title="Infrastructure remediation required",
        description="A synthetic incident requires a proposed configuration change.",
        source="test-fixture",
    )


def make_plan_payload(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Generate and validate the smallest safe configuration change.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate a proposal",
                "description": "Create one declarative infrastructure artifact.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate in isolation",
                "description": "Run schema, lint, and security checks in a sandbox.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Request review",
                "description": "Require human approval before repository operations.",
            },
        ],
    }


def kubernetes_artifact_payload(*, content: str | None = None) -> dict[str, str]:
    return {
        "kind": "kubernetes_manifest",
        "path": "remediations/payment-api.yaml",
        "content": content
        or """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-api
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            limits:
              memory: 512Mi
""",
        "change_summary": "Increase the container memory limit to 512Mi.",
    }


def terraform_artifact_payload() -> dict[str, str]:
    return {
        "kind": "terraform_configuration",
        "path": "remediations/security-group.tf",
        "content": """resource "aws_security_group_rule" "https_ingress" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["10.0.0.0/8"]
  security_group_id = var.security_group_id
}
""",
        "change_summary": "Restrict HTTPS ingress to the private network range.",
    }


def test_workflow_generates_kubernetes_artifact() -> None:
    incident = make_incident(IncidentType.KUBERNETES_OOM_KILLED)
    model = FakeStructuredModel([make_plan_payload(incident), kubernetes_artifact_payload()])

    result = RemediationWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.VALIDATING
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.kind is ArtifactKind.KUBERNETES_MANIFEST
    assert artifact.path == "remediations/payment-api.yaml"
    assert artifact.revision == 1
    assert "memory: 512Mi" in artifact.content
    assert len(artifact.checksum) == 64
    assert [request.response_model for request in model.requests] == [
        "RemediationPlan",
        "ArtifactDraft",
    ]
    assert "kubernetes-openapi-v1.31-curated" in model.requests[1].user_prompt
    assert "resources.limits.memory" in model.requests[1].user_prompt


def test_workflow_generates_terraform_artifact_with_separate_models() -> None:
    incident = make_incident(IncidentType.TERRAFORM_DRIFT)
    planner = FakeStructuredModel([make_plan_payload(incident)])
    generator = FakeStructuredModel([terraform_artifact_payload()])

    result = RemediationWorkflow(planner, generator).invoke(incident)

    assert result.status is WorkflowStatus.VALIDATING
    assert result.artifacts[0].kind is ArtifactKind.TERRAFORM_CONFIGURATION
    assert result.artifacts[0].path.endswith(".tf")
    assert generator.requests[0].response_model == "ArtifactDraft"
    assert "Expected kind: terraform_configuration" in generator.requests[0].user_prompt
    assert "hashicorp/aws" in generator.requests[0].user_prompt
    assert "source_security_group_id" in generator.requests[0].user_prompt


def test_generator_fails_safely_for_wrong_artifact_kind() -> None:
    incident = make_incident(IncidentType.KUBERNETES_CRASH_LOOP)
    model = FakeStructuredModel([make_plan_payload(incident), terraform_artifact_payload()])

    result = RemediationWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.artifacts == ()
    assert result.failure_reason is not None
    assert "terraform_configuration" in result.failure_reason


def test_generator_fails_safely_for_invalid_model_output() -> None:
    incident = make_incident(IncidentType.INVALID_SECURITY_GROUP_RULE)
    invalid = terraform_artifact_payload() | {"path": "../../main.tf"}
    model = FakeStructuredModel([make_plan_payload(incident), invalid])

    result = RemediationWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.failure_reason is not None
    assert "schema validation" in result.failure_reason


def test_generator_fails_closed_when_requested_schema_is_not_approved() -> None:
    incident = make_incident(IncidentType.KUBERNETES_OOM_KILLED).model_copy(
        update={"context": {"k8s_kind": "StatefulSet"}}
    )
    model = FakeStructuredModel([make_plan_payload(incident)])

    result = RemediationWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.artifacts == ()
    assert result.failure_reason is not None
    assert "unsupported Kubernetes schema" in result.failure_reason
    assert len(model.requests) == 1


def test_generator_creates_linked_revision_with_validation_context() -> None:
    incident = make_incident(IncidentType.KUBERNETES_OOM_KILLED)
    plan = RemediationPlan.model_validate(make_plan_payload(incident))
    previous = materialize_artifact(ArtifactDraft.model_validate(kubernetes_artifact_payload()))
    revised_payload = kubernetes_artifact_payload(
        content=kubernetes_artifact_payload()["content"].replace("512Mi", "640Mi")
    )
    model = FakeStructuredModel([revised_payload])
    state = AgentState(
        incident=incident,
        status=WorkflowStatus.GENERATING,
        plan=plan,
        artifacts=(previous,),
        validation_errors=("memory limit must exceed observed peak",),
    )

    result = AgentState.model_validate(state.model_dump() | GeneratorNode(model)(state))

    assert result.status is WorkflowStatus.VALIDATING
    assert len(result.artifacts) == 2
    revision = result.artifacts[-1]
    assert revision.revision == 2
    assert revision.parent_id == previous.id
    assert "memory limit must exceed" in model.requests[0].user_prompt
    assert previous.content in model.requests[0].user_prompt


def test_generator_rejects_unchanged_revision() -> None:
    incident = make_incident(IncidentType.KUBERNETES_OOM_KILLED)
    plan = RemediationPlan.model_validate(make_plan_payload(incident))
    previous = materialize_artifact(ArtifactDraft.model_validate(kubernetes_artifact_payload()))
    state = AgentState(
        incident=incident,
        status=WorkflowStatus.GENERATING,
        plan=plan,
        artifacts=(previous,),
    )

    result = AgentState.model_validate(
        state.model_dump()
        | GeneratorNode(FakeStructuredModel([kubernetes_artifact_payload()]))(state)
    )

    assert result.status is WorkflowStatus.FAILED
    assert result.failure_reason is not None
    assert "must change file content" in result.failure_reason


@pytest.mark.parametrize(
    ("incident_type", "kind"),
    [
        (IncidentType.KUBERNETES_OOM_KILLED, ArtifactKind.KUBERNETES_MANIFEST),
        (IncidentType.KUBERNETES_CRASH_LOOP, ArtifactKind.KUBERNETES_MANIFEST),
        (IncidentType.TERRAFORM_DRIFT, ArtifactKind.TERRAFORM_CONFIGURATION),
        (IncidentType.INVALID_SECURITY_GROUP_RULE, ArtifactKind.TERRAFORM_CONFIGURATION),
    ],
)
def test_artifact_kind_is_selected_from_incident(
    incident_type: IncidentType, kind: ArtifactKind
) -> None:
    assert artifact_kind_for_incident(incident_type) is kind


def test_generator_requires_plan_and_generating_status() -> None:
    state = AgentState(incident=make_incident(IncidentType.KUBERNETES_OOM_KILLED))

    with pytest.raises(ValueError, match="requires generating status"):
        GeneratorNode(FakeStructuredModel([]))(state)
    with pytest.raises(ValueError, match="requires a remediation plan"):
        build_generator_prompts(state, ArtifactKind.KUBERNETES_MANIFEST)


def test_generation_graph_exposes_generator_stage() -> None:
    graph = build_generation_graph(FakeStructuredModel([]))
    drawable = graph.get_graph()

    assert {"prepare_planning", "planner", "generator"}.issubset(drawable.nodes)
    assert any(edge.source == "planner" and edge.target == "generator" for edge in drawable.edges)


def test_generator_routing_requires_artifact_or_failure() -> None:
    incident = make_incident(IncidentType.KUBERNETES_OOM_KILLED)
    valid = RemediationWorkflow(
        FakeStructuredModel([make_plan_payload(incident), kubernetes_artifact_payload()])
    ).invoke(incident)
    failed = AgentState(
        incident=incident,
        status=WorkflowStatus.FAILED,
        failure_reason="generation failed",
    )

    assert route_after_generator(valid) is GeneratorRoute.VALIDATION_READY
    assert route_after_generator(failed) is GeneratorRoute.FAILED
    with pytest.raises(ValueError, match="unroutable"):
        route_after_generator(AgentState(incident=incident))
