"""LangGraph skeleton for incident normalization and remediation planning."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_incident_remediation.incidents import normalize_incident
from agentic_incident_remediation.llm import StructuredModel
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    Incident,
    MessageRole,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.planner import PlannerNode


class PlannerRoute(StrEnum):
    """Deterministic exits from the planner subgraph."""

    GENERATION_READY = "generation_ready"
    FAILED = "failed"


def prepare_planning(state: AgentState) -> dict[str, Any]:
    """Record normalized input and move a newly received incident into planning."""

    if state.status is not WorkflowStatus.RECEIVED:
        raise ValueError("planning workflow requires a newly received incident")
    message = AgentMessage(
        role=MessageRole.USER,
        name="alert-normalizer",
        content=f"Normalized {state.incident.type.value} incident from {state.incident.source}.",
    )
    return {
        "status": WorkflowStatus.PLANNING,
        "messages": (*state.messages, message),
        "updated_at": utc_now(),
    }


def route_after_planner(state: AgentState) -> PlannerRoute:
    """Route only validated plans forward and failures to the terminal edge."""

    if state.status is WorkflowStatus.GENERATING and state.plan is not None:
        return PlannerRoute.GENERATION_READY
    if state.status is WorkflowStatus.FAILED:
        return PlannerRoute.FAILED
    raise ValueError("planner produced an unroutable workflow state")


def build_planner_graph(
    model: StructuredModel,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile the first workflow slice: prepare input, plan, then stop at generation."""

    builder = StateGraph(AgentState)
    builder.add_node("prepare_planning", prepare_planning)
    builder.add_node("planner", PlannerNode(model))
    builder.add_edge(START, "prepare_planning")
    builder.add_edge("prepare_planning", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            PlannerRoute.GENERATION_READY: END,
            PlannerRoute.FAILED: END,
        },
    )
    return builder.compile(name="incident-remediation-planner")


class PlannerWorkflow:
    """Public entry point that normalizes alert payloads before invoking LangGraph."""

    def __init__(self, model: StructuredModel, *, max_retries: int = 3) -> None:
        self._graph = build_planner_graph(model)
        self._max_retries = max_retries

    def invoke(self, alert: Incident | Mapping[str, Any]) -> AgentState:
        incident = normalize_incident(alert)
        initial_state = AgentState(incident=incident, max_retries=self._max_retries)
        result = self._graph.invoke(initial_state)
        return AgentState.model_validate(result)
