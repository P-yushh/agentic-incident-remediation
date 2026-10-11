"""Planner node and LangGraph routing tests."""

from typing import Any

import pytest

from agentic_incident_remediation.graph import (
    PlannerRoute,
    PlannerWorkflow,
    build_planner_graph,
    route_after_planner,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    AgentState,
    Incident,
    IncidentType,
    WorkflowStatus,
)
from agentic_incident_remediation.planner import PlannerNode, build_planner_prompts


def make_incident() -> Incident:
    return Incident(
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title="Payment API pod ran out of memory",
        description="The container exceeded its configured memory limit.",
        source="prometheus",
    )


def make_plan_payload(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Propose a bounded memory-limit adjustment.",
        "steps": [
            {
                "order": 1,
                "kind": "investigate",
                "title": "Inspect recent memory usage",
                "description": "Confirm the limit was exhausted rather than a node failure.",
            },
            {
                "order": 2,
                "kind": "generate",
                "title": "Generate a manifest patch",
                "description": "Increase only the affected container memory limit.",
            },
            {
                "order": 3,
                "kind": "validate",
                "title": "Validate in the sandbox",
                "description": "Run Kubernetes schema, policy, and security checks.",
            },
            {
                "order": 4,
                "kind": "request_approval",
                "title": "Request human approval",
                "description": "Present the validated proposal before any repository write.",
            },
        ],
        "assumptions": ["Observed usage supports a modest limit increase."],
        "risk_notes": ["A higher limit can increase node memory pressure."],
    }


def test_workflow_normalizes_alert_and_generates_plan() -> None:
    alert = {
        "source": "prometheus",
        "type": "OOMKilled",
        "title": "Payment API pod ran out of memory",
        "description": "The container exceeded its configured memory limit.",
        "labels": {"namespace": "payments", "severity": "high"},
    }
    normalized = make_incident()
    alert["id"] = str(normalized.id)
    model = FakeStructuredModel([make_plan_payload(normalized)])

    result = PlannerWorkflow(model).invoke(alert)

    assert result.status is WorkflowStatus.GENERATING
    assert result.plan is not None
    assert result.plan.incident_id == result.incident.id
    assert [message.name for message in result.messages] == ["alert-normalizer", "planner"]
    assert len(model.requests) == 1
    assert "Do not include an infrastructure apply operation" in model.requests[0].user_prompt


def test_workflow_converts_invalid_model_output_to_failed_state() -> None:
    incident = make_incident()
    model = FakeStructuredModel([{"incident_id": str(incident.id), "summary": "Incomplete"}])

    result = PlannerWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.plan is None
    assert result.failure_reason is not None
    assert "schema validation" in result.failure_reason
    assert result.messages[-1].name == "planner"


def test_workflow_rejects_plan_for_different_incident() -> None:
    incident = make_incident()
    other = make_incident()
    model = FakeStructuredModel([make_plan_payload(other)])

    result = PlannerWorkflow(model).invoke(incident)

    assert result.status is WorkflowStatus.FAILED
    assert result.failure_reason is not None
    assert "different incident" in result.failure_reason


def test_graph_exposes_named_planning_nodes() -> None:
    graph = build_planner_graph(FakeStructuredModel([]))

    drawable = graph.get_graph()

    assert {"prepare_planning", "planner"}.issubset(drawable.nodes)
    assert any(
        edge.source == "prepare_planning" and edge.target == "planner" for edge in drawable.edges
    )


def test_planner_node_requires_planning_status() -> None:
    state = AgentState(incident=make_incident())

    with pytest.raises(ValueError, match="requires workflow status planning"):
        PlannerNode(FakeStructuredModel([]))(state)


def test_route_after_planner_is_deterministic() -> None:
    incident = make_incident()
    ready_result = PlannerWorkflow(FakeStructuredModel([make_plan_payload(incident)])).invoke(
        incident
    )
    failed_state = AgentState(
        incident=incident,
        status=WorkflowStatus.FAILED,
        failure_reason="planning failed",
    )

    assert route_after_planner(ready_result) is PlannerRoute.GENERATION_READY
    assert route_after_planner(failed_state) is PlannerRoute.FAILED
    with pytest.raises(ValueError, match="unroutable"):
        route_after_planner(AgentState(incident=incident))


def test_planner_prompts_include_normalized_incident_and_safety_rules() -> None:
    incident = make_incident()

    system_prompt, user_prompt = build_planner_prompts(incident)

    assert "request human approval as its final step" in system_prompt
    assert str(incident.id) in user_prompt
    assert incident.type.value in user_prompt
    assert "apply operation" in user_prompt


def test_workflow_preserves_configured_retry_limit() -> None:
    incident = make_incident()
    result = PlannerWorkflow(
        FakeStructuredModel([make_plan_payload(incident)]), max_retries=2
    ).invoke(incident)

    assert result.max_retries == 2
