"""Durable remediation graph with native human approval interruption."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, Command

from agentic_incident_remediation.approval import ApprovalGateNode, PrepareApprovalNode
from agentic_incident_remediation.generator import GeneratorNode
from agentic_incident_remediation.graph.workflow import (
    GeneratorRoute,
    PlannerRoute,
    ReflectorRoute,
    ValidatorRoute,
    prepare_planning,
    route_after_generator,
    route_after_planner,
    route_after_reflector,
    route_after_validator,
)
from agentic_incident_remediation.incidents import normalize_incident
from agentic_incident_remediation.llm import StructuredModel
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ApprovalDecision,
    ApprovalResponse,
    Incident,
    MessageRole,
    WorkflowStatus,
)
from agentic_incident_remediation.models.base import utc_now
from agentic_incident_remediation.operations import PullRequestExecutorNode, PullRequestPublisher
from agentic_incident_remediation.persistence import checkpoint_config
from agentic_incident_remediation.planner import PlannerNode
from agentic_incident_remediation.reflector import ReflectorNode
from agentic_incident_remediation.sandbox import SandboxRunner, ValidatorNode
from agentic_incident_remediation.schema_tools.selection import SchemaToolProvider


class ApprovalRoute(StrEnum):
    """Deterministic exits from the human approval interruption."""

    APPROVED = "approved"
    REJECTED = "rejected"


def route_after_approval(state: AgentState) -> ApprovalRoute:
    """Permit repository operations only after an explicit matching approval."""

    if (
        state.status is WorkflowStatus.APPROVED
        and state.approval is not None
        and state.approval.decision is ApprovalDecision.APPROVED
    ):
        return ApprovalRoute.APPROVED
    if (
        state.status is WorkflowStatus.REJECTED
        and state.approval is not None
        and state.approval.decision is ApprovalDecision.REJECTED
    ):
        return ApprovalRoute.REJECTED
    raise ValueError("approval gate produced an unroutable workflow state")


def build_approval_graph(
    planner_model: StructuredModel,
    sandbox_runner: SandboxRunner,
    publisher: PullRequestPublisher,
    generator_model: StructuredModel | None = None,
    *,
    repository: str,
    base_branch: str = "main",
    schema_tools: SchemaToolProvider | None = None,
    checkpointer: Checkpointer,
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile the full workflow through approval and safe PR publication."""

    if not checkpointer:
        raise ValueError("approval workflow requires a checkpointer")

    builder = StateGraph(AgentState)
    builder.add_node("prepare_planning", prepare_planning)
    builder.add_node("planner", PlannerNode(planner_model))
    builder.add_node(
        "generator", GeneratorNode(generator_model or planner_model, schema_tools=schema_tools)
    )
    builder.add_node("validator", ValidatorNode(sandbox_runner))
    builder.add_node("reflector", ReflectorNode())
    builder.add_node("prepare_approval", PrepareApprovalNode())
    builder.add_node("approval_gate", ApprovalGateNode())
    builder.add_node(
        "pull_request_executor",
        PullRequestExecutorNode(
            publisher,
            repository=repository,
            base_branch=base_branch,
        ),
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
            GeneratorRoute.VALIDATION_READY: "validator",
            GeneratorRoute.FAILED: END,
        },
    )
    builder.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            ValidatorRoute.APPROVAL_READY: "prepare_approval",
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
    builder.add_edge("prepare_approval", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {
            ApprovalRoute.APPROVED: "pull_request_executor",
            ApprovalRoute.REJECTED: END,
        },
    )
    builder.add_edge("pull_request_executor", END)
    return builder.compile(
        checkpointer=checkpointer,
        name="incident-remediation-approval",
    )


class DurableApprovalWorkflow:
    """Start, inspect, and resume persisted remediation approval executions."""

    def __init__(
        self,
        planner_model: StructuredModel,
        sandbox_runner: SandboxRunner,
        publisher: PullRequestPublisher,
        generator_model: StructuredModel | None = None,
        *,
        repository: str,
        base_branch: str = "main",
        schema_tools: SchemaToolProvider | None = None,
        checkpointer: Checkpointer,
        max_retries: int = 3,
    ) -> None:
        self._graph = build_approval_graph(
            planner_model,
            sandbox_runner,
            publisher,
            generator_model,
            repository=repository,
            base_branch=base_branch,
            schema_tools=schema_tools,
            checkpointer=checkpointer,
        )
        self._max_retries = max_retries

    def start(
        self,
        alert: Incident | Mapping[str, Any],
        *,
        execution_id: UUID | None = None,
    ) -> AgentState:
        """Run through validation and pause at the durable approval gate."""

        incident = normalize_incident(alert)
        initial_state = AgentState(
            execution_id=execution_id or uuid4(),
            incident=incident,
            max_retries=self._max_retries,
        )
        # Persist each checkpoint before its tasks write results or approval interrupts.
        # Redis pending-write metadata must not race with an asynchronous checkpoint save.
        self._graph.invoke(
            initial_state, config=self._config(initial_state.execution_id), durability="sync"
        )
        return self.get_state(initial_state.execution_id)

    def resume(
        self,
        execution_id: UUID,
        response: ApprovalResponse | Mapping[str, Any],
    ) -> AgentState:
        """Resume one pending interrupt with a correlated human decision."""

        decision = ApprovalResponse.model_validate(response)
        current = self.get_state(execution_id)
        if current.control_status != "running":
            raise ValueError("execution is paused or stopping")
        if current.status is not WorkflowStatus.AWAITING_APPROVAL or current.approval is None:
            raise ValueError("execution is not awaiting approval")
        if decision.approval_id != current.approval.id:
            raise ValueError("approval response does not match the pending request")
        command: Command[Any] = Command(resume=decision.model_dump(mode="json"))
        self._graph.invoke(
            command,
            config=self._config(execution_id),
            durability="sync",
        )
        return self.get_state(execution_id)

    def prepare(self, alert: Incident | Mapping[str, Any]) -> AgentState:
        """Persist a new incident before any planning or external work begins."""

        initial = AgentState(incident=normalize_incident(alert), max_retries=self._max_retries)
        self._graph.invoke(
            initial,
            config=self._config(initial.execution_id),
            interrupt_before=["prepare_planning"],
            durability="sync",
        )
        return self.get_state(initial.execution_id)

    def advance(self, execution_id: UUID) -> AgentState:
        """Run at most one checkpointed node, preserving the human approval interrupt."""

        current = self.get_state(execution_id)
        if current.control_status != "running":
            raise ValueError("execution is paused or stopping")
        snapshot = self._graph.get_state(self._config(execution_id))
        if snapshot.next and not snapshot.interrupts:
            self._graph.invoke(
                None, config=self._config(execution_id), interrupt_after="*", durability="sync"
            )
        return self.get_state(execution_id)

    def ready_for_review(self, execution_id: UUID) -> bool:
        """Distinguish the approval interrupt from a node-boundary checkpoint."""

        snapshot = self._graph.get_state(self._config(execution_id))
        return bool(snapshot.interrupts)

    def set_control(
        self, execution_id: UUID, action: Literal["pause", "resume", "terminate", "fail"]
    ) -> AgentState:
        """Checkpoint a control at a safe node boundary; termination clears pending work."""

        current = self.get_state(execution_id)
        if current.status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.REJECTED,
            WorkflowStatus.FAILED,
            WorkflowStatus.TERMINATED,
        }:
            raise ValueError("execution has already finished")
        update: dict[str, Any] = {
            "control_status": "paused" if action == "pause" else "running",
            "updated_at": utc_now(),
            "messages": (
                *current.messages,
                AgentMessage(role=MessageRole.USER, name="execution-control", content=action),
            ),
        }
        if action in {"terminate", "fail"}:
            update["status"] = (
                WorkflowStatus.TERMINATED if action == "terminate" else WorkflowStatus.FAILED
            )
            if action == "fail":
                update["failure_reason"] = "background execution could not complete safely"
            self._graph.update_state(
                self._config(execution_id), update, as_node="pull_request_executor"
            )
        else:
            self._graph.update_state(self._config(execution_id), update)
        return self.get_state(execution_id)

    def get_state(self, execution_id: UUID) -> AgentState:
        """Restore the latest checkpoint for an execution."""

        snapshot = self._graph.get_state(self._config(execution_id))
        if not snapshot.values:
            raise LookupError(f"execution {execution_id} was not found")
        return AgentState.model_validate(snapshot.values)

    def get_history(self, execution_id: UUID) -> tuple[AgentState, ...]:
        """Return newest-first snapshots for audit inspection."""

        return tuple(
            AgentState.model_validate(snapshot.values)
            for snapshot in self._graph.get_state_history(self._config(execution_id))
            if snapshot.values
        )

    def _config(self, execution_id: UUID) -> RunnableConfig:
        return checkpoint_config(
            execution_id,
            recursion_limit=14 + self._max_retries * 4,
        )
