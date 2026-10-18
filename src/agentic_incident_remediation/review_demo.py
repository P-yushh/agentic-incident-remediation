"""One fresh synthetic incident, with the original checkpoint resumed after review."""

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.models import (
    AgentState,
    PullRequestProposal,
    PullRequestRecord,
    WorkflowStatus,
)
from agentic_incident_remediation.operations.publisher import OperationsError
from agentic_incident_remediation.review_scenarios import (
    SCENARIOS,
    DemoScenario,
    scenario_for_manifest,
)

DEMO_INCIDENT = SCENARIOS[DemoScenario.OOM].incident()


class PublicationDisabled:
    """Preparation has no GitHub writer, even if the graph were routed incorrectly."""

    def publish(self, proposal: PullRequestProposal) -> PullRequestRecord:
        raise OperationsError("publication is disabled before human review")


def require_pending_review(workflow: DurableApprovalWorkflow, state: AgentState) -> None:
    """Check the actual interrupt and validation evidence, not just a status label."""

    if (
        state.status is not WorkflowStatus.AWAITING_APPROVAL
        or state.approval is None
        or state.approval.decision != "pending"
        or not state.review_ready
        or not workflow.ready_for_review(state.execution_id)
        or len(state.artifacts) != 1
        or state.approval.artifact_ids != (state.artifacts[0].id,)
        or state.pull_request is not None
    ):
        raise ValueError("execution is not a single-artifact pending native review")
    results = [r for r in state.validation_results if r.artifact_id == state.artifacts[0].id]
    if not results or any(r.status != "passed" or r.exit_code != 0 for r in results):
        raise ValueError("latest artifact did not pass all real validation checks")
    if len(workflow.get_history(state.execution_id)) < 2:
        raise ValueError("checkpoint history is missing")


def prepare_review(
    workflow: DurableApprovalWorkflow,
    budget: BudgetedModel,
    provenance: dict[str, str],
    scenario: DemoScenario = DemoScenario.OOM,
) -> dict[str, Any]:
    """Start from a fresh alert and save review evidence without deciding for the user."""

    with TestClient(create_app(RemediationService(workflow))) as client:
        response = client.post("/api/incidents", json=SCENARIOS[scenario].incident())
        response.raise_for_status()
        state = AgentState.model_validate(response.json())
        require_pending_review(workflow, state)
    if budget.calls != 2:
        raise ValueError("the demo requires exactly one plan and one artifact invocation")
    return {
        "version": 2,
        "scenario": scenario.value,
        "synthetic": True,
        "provenance": provenance,
        "model_calls": budget.calls,
        "artifact_sha256": state.artifacts[0].checksum,
        "state": state.model_dump(mode="json"),
    }


def publish_review(
    workflow: DurableApprovalWorkflow, manifest: dict[str, Any], *, reviewer: str
) -> dict[str, Any]:
    """Resume the same persisted interrupt; never seed or reconstruct a new graph state."""

    if manifest.get("synthetic") is not True:
        raise ValueError("unsupported review manifest")
    scenario_for_manifest(manifest)
    expected = AgentState.model_validate(manifest["state"])
    restored = workflow.get_state(expected.execution_id).model_copy(
        update={"review_ready": workflow.ready_for_review(expected.execution_id)}
    )
    if restored.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("restored execution differs from the reviewed checkpoint")
    require_pending_review(workflow, restored)
    if restored.artifacts[0].checksum != manifest["artifact_sha256"]:
        raise ValueError("reviewed artifact checksum does not match")
    assert restored.approval is not None
    with TestClient(create_app(RemediationService(workflow))) as client:
        response = client.post(
            f"/api/executions/{UUID(str(restored.execution_id))}/approval",
            json={
                "approval_id": str(restored.approval.id),
                "decision": "approved",
                "decided_by": reviewer,
                "comment": "Approved the synthetic review bundle through GitHub required review.",
            },
        )
        response.raise_for_status()
        completed = AgentState.model_validate(response.json())
    if completed.status is not WorkflowStatus.COMPLETED or completed.pull_request is None:
        raise ValueError("publication did not complete; inspect before attempting any recovery")
    return {
        "execution_id": str(completed.execution_id),
        "approval_id": str(restored.approval.id),
        "reviewer": reviewer,
        "artifact_sha256": completed.artifacts[0].checksum,
        "publication_model_calls": 0,
        "pull_request": completed.pull_request.model_dump(mode="json"),
        "status": completed.status.value,
        "merge_performed": False,
        "deployment_performed": False,
    }
