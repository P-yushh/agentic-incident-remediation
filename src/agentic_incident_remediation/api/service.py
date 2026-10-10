"""Application service around the durable workflow."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agentic_incident_remediation.api.observability import ApiMetrics
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.models import AgentState, ApprovalResponse


class RemediationService:
    """Expose workflow operations while recording business-level telemetry."""

    def __init__(
        self,
        workflow: DurableApprovalWorkflow,
        metrics: ApiMetrics | None = None,
    ) -> None:
        self.workflow = workflow
        self.metrics = metrics or ApiMetrics()

    def start(self, incident: Mapping[str, Any]) -> AgentState:
        state = self.workflow.start(incident)
        self.metrics.record_start(state.status)
        return state

    def get(self, execution_id: UUID) -> AgentState:
        return self.workflow.get_state(execution_id)

    def history(self, execution_id: UUID) -> tuple[AgentState, ...]:
        return self.workflow.get_history(execution_id)

    def decide(self, execution_id: UUID, response: ApprovalResponse) -> AgentState:
        state = self.workflow.resume(execution_id, response)
        self.metrics.record_decision(response.decision, state.status)
        return state
