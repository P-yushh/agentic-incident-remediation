"""LangGraph skeleton for incident normalization and remediation planning."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_incident_remediation.generator import GeneratorNode
from agentic_incident_remediation.incidents import normalize_incident
from agentic_incident_remediation.llm import StructuredModel
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    Incident,
    MessageRole,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.planner import PlannerNode
from agentic_incident_remediation.reflector import ReflectorNode
from agentic_incident_remediation.sandbox import SandboxRunner, ValidatorNode
from agentic_incident_remediation.schema_tools.selection import SchemaToolProvider


class PlannerRoute(StrEnum):
    """Deterministic exits from the planner subgraph."""

    GENERATION_READY = "generation_ready"
    FAILED = "failed"


class GeneratorRoute(StrEnum):
    """Deterministic exits from the artifact generator."""

    VALIDATION_READY = "validation_ready"
    FAILED = "failed"


class ValidatorRoute(StrEnum):
    """Deterministic exits from isolated artifact validation."""

    APPROVAL_READY = "approval_ready"
    REFLECTION_READY = "reflection_ready"
    FAILED = "failed"


class ReflectorRoute(StrEnum):
    """Deterministic exits from bounded validation reflection."""

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


def route_after_generator(state: AgentState) -> GeneratorRoute:
    """Route only a generated artifact toward the future validation layer."""

    if state.status is WorkflowStatus.VALIDATING and state.artifacts:
        return GeneratorRoute.VALIDATION_READY
    if state.status is WorkflowStatus.FAILED:
        return GeneratorRoute.FAILED
    raise ValueError("generator produced an unroutable workflow state")


def route_after_validator(state: AgentState) -> ValidatorRoute:
    """Route validated artifacts to the next unimplemented workflow boundary."""

    if state.status is WorkflowStatus.FAILED:
        return ValidatorRoute.FAILED
    if not state.artifacts:
        raise ValueError("validator routing requires an artifact")
    artifact_id = state.artifacts[-1].id
    current_results = tuple(
        result for result in state.validation_results if result.artifact_id == artifact_id
    )
    if (
        state.status is WorkflowStatus.AWAITING_APPROVAL
        and current_results
        and all(result.status is ValidationStatus.PASSED for result in current_results)
    ):
        return ValidatorRoute.APPROVAL_READY
    if (
        state.status is WorkflowStatus.REFLECTING
        and any(result.status is ValidationStatus.FAILED for result in current_results)
        and state.validation_errors
    ):
        return ValidatorRoute.REFLECTION_READY
    raise ValueError("validator produced an unroutable workflow state")


def route_after_reflector(state: AgentState) -> ReflectorRoute:
    """Route a bounded retry to generation or stop after ceiling exhaustion."""

    if (
        state.status is WorkflowStatus.GENERATING
        and state.retry_count > 0
        and state.validation_errors
    ):
        return ReflectorRoute.GENERATION_READY
    if state.status is WorkflowStatus.FAILED:
        return ReflectorRoute.FAILED
    raise ValueError("reflector produced an unroutable workflow state")


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


def build_generation_graph(
    planner_model: StructuredModel,
    generator_model: StructuredModel | None = None,
    *,
    schema_tools: SchemaToolProvider | None = None,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile normalization, planning, and artifact generation workflow stages."""

    builder = StateGraph(AgentState)
    builder.add_node("prepare_planning", prepare_planning)
    builder.add_node("planner", PlannerNode(planner_model))
    builder.add_node(
        "generator", GeneratorNode(generator_model or planner_model, schema_tools=schema_tools)
    )
    builder.add_edge(START, "prepare_planning")
    builder.add_edge("prepare_planning", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            PlannerRoute.GENERATION_READY: "generator",
            PlannerRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "generator",
        route_after_generator,
        {
            GeneratorRoute.VALIDATION_READY: END,
            GeneratorRoute.FAILED: END,
        },
    )
    return builder.compile(name="incident-remediation-generation")


def build_validation_graph(
    planner_model: StructuredModel,
    sandbox_runner: SandboxRunner,
    generator_model: StructuredModel | None = None,
    *,
    schema_tools: SchemaToolProvider | None = None,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile the workflow through isolated validation and stop at the next boundary."""

    builder = StateGraph(AgentState)
    builder.add_node("prepare_planning", prepare_planning)
    builder.add_node("planner", PlannerNode(planner_model))
    builder.add_node(
        "generator", GeneratorNode(generator_model or planner_model, schema_tools=schema_tools)
    )
    builder.add_node("validator", ValidatorNode(sandbox_runner))
    builder.add_edge(START, "prepare_planning")
    builder.add_edge("prepare_planning", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            PlannerRoute.GENERATION_READY: "generator",
            PlannerRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "generator",
        route_after_generator,
        {
            GeneratorRoute.VALIDATION_READY: "validator",
            GeneratorRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            ValidatorRoute.APPROVAL_READY: END,
            ValidatorRoute.REFLECTION_READY: END,
            ValidatorRoute.FAILED: END,
        },
    )
    return builder.compile(name="incident-remediation-validation")


def build_reflection_graph(
    planner_model: StructuredModel,
    sandbox_runner: SandboxRunner,
    generator_model: StructuredModel | None = None,
    *,
    schema_tools: SchemaToolProvider | None = None,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile planning, generation, validation, and bounded self-correction."""

    builder = StateGraph(AgentState)
    builder.add_node("prepare_planning", prepare_planning)
    builder.add_node("planner", PlannerNode(planner_model))
    builder.add_node(
        "generator", GeneratorNode(generator_model or planner_model, schema_tools=schema_tools)
    )
    builder.add_node("validator", ValidatorNode(sandbox_runner))
    builder.add_node("reflector", ReflectorNode())
    builder.add_edge(START, "prepare_planning")
    builder.add_edge("prepare_planning", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            PlannerRoute.GENERATION_READY: "generator",
            PlannerRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "generator",
        route_after_generator,
        {
            GeneratorRoute.VALIDATION_READY: "validator",
            GeneratorRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            ValidatorRoute.APPROVAL_READY: END,
            ValidatorRoute.REFLECTION_READY: "reflector",
            ValidatorRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "reflector",
        route_after_reflector,
        {
            ReflectorRoute.GENERATION_READY: "generator",
            ReflectorRoute.FAILED: END,
        },
    )
    return builder.compile(name="incident-remediation-reflection")


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


class RemediationWorkflow:
    """Workflow entry point that currently stops at the validation boundary."""

    def __init__(
        self,
        planner_model: StructuredModel,
        generator_model: StructuredModel | None = None,
        *,
        schema_tools: SchemaToolProvider | None = None,
        max_retries: int = 3,
    ) -> None:
        self._graph = build_generation_graph(
            planner_model,
            generator_model,
            schema_tools=schema_tools,
        )
        self._max_retries = max_retries

    def invoke(self, alert: Incident | Mapping[str, Any]) -> AgentState:
        incident = normalize_incident(alert)
        initial_state = AgentState(incident=incident, max_retries=self._max_retries)
        result = self._graph.invoke(initial_state)
        return AgentState.model_validate(result)


class SandboxedRemediationWorkflow:
    """Workflow entry point that stops after isolated artifact validation."""

    def __init__(
        self,
        planner_model: StructuredModel,
        sandbox_runner: SandboxRunner,
        generator_model: StructuredModel | None = None,
        *,
        schema_tools: SchemaToolProvider | None = None,
        max_retries: int = 3,
    ) -> None:
        self._graph = build_validation_graph(
            planner_model,
            sandbox_runner,
            generator_model,
            schema_tools=schema_tools,
        )
        self._max_retries = max_retries

    def invoke(self, alert: Incident | Mapping[str, Any]) -> AgentState:
        incident = normalize_incident(alert)
        initial_state = AgentState(incident=incident, max_retries=self._max_retries)
        result = self._graph.invoke(initial_state)
        return AgentState.model_validate(result)


class SelfCorrectingRemediationWorkflow:
    """Workflow entry point with bounded validation-driven artifact correction."""

    def __init__(
        self,
        planner_model: StructuredModel,
        sandbox_runner: SandboxRunner,
        generator_model: StructuredModel | None = None,
        *,
        schema_tools: SchemaToolProvider | None = None,
        max_retries: int = 3,
    ) -> None:
        self._graph = build_reflection_graph(
            planner_model,
            sandbox_runner,
            generator_model,
            schema_tools=schema_tools,
        )
        self._max_retries = max_retries

    def invoke(self, alert: Incident | Mapping[str, Any]) -> AgentState:
        incident = normalize_incident(alert)
        initial_state = AgentState(incident=incident, max_retries=self._max_retries)
        result = self._graph.invoke(
            initial_state,
            config={"recursion_limit": 10 + self._max_retries * 4},
        )
        return AgentState.model_validate(result)
