"""Explicit experimental loopback inference; no credentials, native execution or auto fallback."""

from collections.abc import Callable

import httpx

from agentic_incident_remediation.generator.node import GeneratorNode
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.llm.local_handoff import LocalProposal
from agentic_incident_remediation.llm.ollama import OllamaStructuredModel
from agentic_incident_remediation.models import AgentState, ArtifactDraft, Incident, WorkflowStatus
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario


def local_readiness() -> None:
    """Check installed local weights only. Never pull, warm up or call a hosted provider."""
    try:
        with httpx.Client(base_url="http://127.0.0.1:11434", trust_env=False, timeout=5) as client:
            tags = client.get("/api/tags")
            tags.raise_for_status()
            if not any(m.get("name") == "qwen3:8b" for m in tags.json()["models"]):
                raise ValueError("qwen3:8b is not installed locally")
            show = client.post("/api/show", json={"model": "qwen3:8b"})
            show.raise_for_status()
            if show.json().get("remote_host") or show.json().get("remote_model"):
                raise ValueError("cloud model aliases are not allowed")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError(
            "Start Ollama with the installed local qwen3:8b model; no cloud fallback"
        ) from None


def generate_proposal(
    scenario: DemoScenario,
    *,
    output_tokens: int,
    timeout_seconds: int,
    should_stop: Callable[[], bool] = lambda: False,
) -> LocalProposal:
    """Use the exact production nodes and schema checks; never repair output or skip validation."""
    from agentic_incident_remediation.planner.node import PlannerNode

    state = AgentState(
        incident=Incident.model_validate(SCENARIOS[scenario].incident()),
        status=WorkflowStatus.PLANNING,
    )
    with OllamaStructuredModel(
        max_output_tokens=output_tokens, timeout_seconds=timeout_seconds, context_tokens=16384
    ) as model:
        budget = BudgetedModel(model)
        if should_stop():
            raise ValueError("local generation cancelled")
        state = state.model_copy(update=PlannerNode(budget)(state))
        if should_stop():
            raise ValueError("local generation cancelled")
        if state.status is WorkflowStatus.GENERATING:
            state = state.model_copy(update=GeneratorNode(budget)(state))
        if state.status is not WorkflowStatus.VALIDATING or state.plan is None:
            raise ValueError(state.failure_reason or "local generation failed")
        return LocalProposal(
            scenario=scenario,
            incident_id=state.incident.id,
            occurred_at=state.incident.occurred_at,
            output_tokens=output_tokens,
            plan=state.plan,
            artifact=ArtifactDraft.model_validate(
                state.artifacts[0].model_dump(include={"kind", "path", "content", "change_summary"})
            ),
        )
