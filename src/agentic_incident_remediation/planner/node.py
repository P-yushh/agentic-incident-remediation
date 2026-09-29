"""LangGraph-compatible planner node."""

from typing import Any

from agentic_incident_remediation.llm import ModelGatewayError, StructuredModel
from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    MessageRole,
    RemediationPlan,
    WorkflowStatus,
)
from agentic_incident_remediation.planner.prompts import build_planner_prompts
from agentic_incident_remediation.state import add_message, transition_state


class PlannerNode:
    """Generate a validated plan and convert model failures into terminal state."""

    def __init__(self, model: StructuredModel) -> None:
        self._model = model

    def __call__(self, state: AgentState) -> dict[str, Any]:
        if state.status is not WorkflowStatus.PLANNING:
            raise ValueError("planner node requires workflow status planning")

        system_prompt, user_prompt = build_planner_prompts(state.incident)
        try:
            plan = self._model.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=RemediationPlan,
            )
            if plan.incident_id != state.incident.id:
                raise ModelGatewayError("planner returned a plan for a different incident")
        except ModelGatewayError as error:
            reason = f"planner could not produce a valid remediation plan: {error}"
            failed = transition_state(state, WorkflowStatus.FAILED, failure_reason=reason)
            failed = add_message(
                failed,
                AgentMessage(role=MessageRole.ASSISTANT, name="planner", content=reason),
            )
            return _state_update(failed, include_plan=False)

        planned = state.model_copy(update={"plan": plan})
        planned = add_message(
            planned,
            AgentMessage(
                role=MessageRole.ASSISTANT,
                name="planner",
                content=f"Prepared a {len(plan.steps)}-step remediation plan.",
            ),
        )
        planned = transition_state(planned, WorkflowStatus.GENERATING)
        return _state_update(planned, include_plan=True)


def _state_update(state: AgentState, *, include_plan: bool) -> dict[str, Any]:
    update: dict[str, Any] = {
        "status": state.status,
        "messages": state.messages,
        "failure_reason": state.failure_reason,
        "updated_at": state.updated_at,
    }
    if include_plan:
        update["plan"] = state.plan
    return update
