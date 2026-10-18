"""Explicitly opted-in Gemini + Docker + Redis smoke test; never authorizes a PR."""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse
from uuid import UUID

from fastapi.testclient import TestClient
from langgraph.types import Checkpointer
from pydantic import BaseModel

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import (
    FakeStructuredModel,
    GeminiStructuredModel,
    ModelGatewayError,
    StructuredModel,
)
from agentic_incident_remediation.models import PullRequestProposal, PullRequestRecord
from agentic_incident_remediation.operations.publisher import OperationsError
from agentic_incident_remediation.persistence import create_redis_checkpointer
from agentic_incident_remediation.sandbox import DockerSandboxRunner, SandboxRunner
from agentic_incident_remediation.schema_tools import LocalSchemaToolProvider

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
CALLS_PER_CASE = 2


class BudgetedModel:
    """Allow exactly one plan and one artifact invocation; never retry a live call."""

    def __init__(self, delegate: StructuredModel) -> None:
        self.delegate = delegate
        self.calls = 0

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseModel],
    ) -> ResponseModel:
        if self.calls >= CALLS_PER_CASE:
            raise ModelGatewayError("live smoke-test request budget exhausted; no request sent")
        self.calls += 1
        return self.delegate.generate_structured(
            system_prompt=system_prompt, user_prompt=user_prompt, response_model=response_model
        )


class NoPublication:
    """No real repository client or write credential is present in this test."""

    def __init__(self) -> None:
        self.attempts = 0

    def publish(self, proposal: PullRequestProposal) -> PullRequestRecord:
        self.attempts += 1
        raise OperationsError("repository publication is disabled in live smoke tests")


def synthetic_incidents() -> tuple[dict[str, Any], ...]:
    """Provide only public, synthetic resource context, never a production manifest."""

    return (
        {
            "type": "kubernetes_oom_killed",
            "title": "Synthetic payment-api OOM event",
            "description": (
                "Synthetic test. Prepare a concise three-step generate/validate/review plan. "
                "Propose a complete apps/v1 Deployment named payment-api in namespace demo. "
                "Use replicas 1, selector and Pod label app=payment-api, container name api, "
                "image example/payment-api:1.0. Increase its memory request from 128Mi to "
                "256Mi and limit from 256Mi to 512Mi. Do not apply anything."
            ),
            "source": "manual-combined-live-workflow",
            "context": {"k8s_kind": "Deployment", "k8s_api_version": "apps/v1"},
        },
        {
            "type": "invalid_security_group_rule",
            "title": "Synthetic overly broad HTTPS ingress",
            "description": (
                "Synthetic test. Prepare a concise three-step generate/validate/review plan. "
                "Propose exactly one aws_security_group_rule resource named web_https, "
                "type ingress, from_port 443, to_port 443, protocol tcp, literal synthetic "
                "security_group_id sg-0123456789abcdef0. Narrow cidr_blocks from public "
                'ingress to ["10.20.0.0/16"]. No providers, variables, data sources, or '
                "provisioners. Do not apply anything."
            ),
            "source": "manual-combined-live-workflow",
            "context": {
                "terraform_provider": "hashicorp/aws",
                "terraform_resource": "aws_security_group_rule",
            },
        },
    )


def make_workflow(
    model: StructuredModel, runner: SandboxRunner, checkpointer: Checkpointer
) -> tuple[DurableApprovalWorkflow, NoPublication]:
    publisher = NoPublication()
    return (
        DurableApprovalWorkflow(
            model,
            runner,
            publisher,
            repository="example/synthetic-live-verification",
            schema_tools=LocalSchemaToolProvider(),
            checkpointer=checkpointer,
            max_retries=1,
        ),
        publisher,
    )


def review_checks(state: dict[str, Any]) -> dict[str, bool]:
    """A state label alone is not evidence of a validated native review interrupt."""

    artifacts = state.get("artifacts", [])
    artifact_id = artifacts[-1]["id"] if artifacts else None
    validations = [
        item for item in state.get("validation_results", []) if item["artifact_id"] == artifact_id
    ]
    approval = state.get("approval")
    return {
        "awaiting_approval": state["status"] == "awaiting_approval",
        "native_interrupt_ready": state["review_ready"] is True,
        "artifact_present": bool(artifacts),
        "latest_artifact_passed": bool(validations)
        and all(item["status"] == "passed" for item in validations),
        "human_decision_pending": bool(approval and approval["decision"] == "pending"),
        "no_pull_request": state["pull_request"] is None,
    }


def run_cases(
    model: StructuredModel, runner: SandboxRunner, checkpointer: Checkpointer
) -> dict[str, Any]:
    incidents = synthetic_incidents()
    if len(incidents) != 2:
        raise ValueError("exactly two synthetic scenarios are required by the four-call budget")
    cases = []
    for incident in incidents:
        budget = BudgetedModel(model)
        workflow, publisher = make_workflow(budget, runner, checkpointer)
        with TestClient(create_app(RemediationService(workflow))) as client:
            response = client.post("/api/incidents", json=incident)
            if response.status_code != 202:
                raise RuntimeError("incident endpoint did not accept the synthetic incident")
            state = response.json()
            checks = review_checks(state)
            checks["no_publication_attempt"] = publisher.attempts == 0
            checks["one_plan_and_one_artifact_call"] = budget.calls == CALLS_PER_CASE
            cases.append(
                {
                    "incident_type": incident["type"],
                    "model_calls": budget.calls,
                    "checks": checks,
                    "passed": all(checks.values()),
                    "state": state,
                }
            )
    return {
        "phase": "run",
        "model": "gemini-3.8-flash",
        "model_calls": sum(case["model_calls"] for case in cases),
        "maximum_model_calls": 4,
        "cases": cases,
        "passed": all(case["passed"] for case in cases),
    }


def recover_cases(
    report: dict[str, Any], runner: SandboxRunner, checkpointer: Checkpointer
) -> dict[str, Any]:
    """Restore in another process, with no model credential or human decision submitted."""

    workflow, publisher = make_workflow(FakeStructuredModel([]), runner, checkpointer)
    with TestClient(create_app(RemediationService(workflow))) as client:
        for case in report["cases"]:
            previous = case["state"]
            execution_id = UUID(previous["execution_id"])
            response = client.get(f"/api/executions/{execution_id}")
            if response.status_code != 200:
                raise RuntimeError("persisted execution could not be restored")
            state = response.json()
            checks = review_checks(state)
            checks["exact_checkpoint_restored"] = state == previous
            checks["checkpoint_history_present"] = len(workflow.get_history(execution_id)) > 1
            checks["no_publication_attempt"] = publisher.attempts == 0
            case["recovery_checks"] = checks
            case["passed"] = case["passed"] and all(checks.values())
    report["phase"] = "recovered"
    report["recovery_model_calls"] = 0
    report["passed"] = all(case["passed"] for case in report["cases"])
    return report


def require_opt_in(phase: str) -> str:
    """Reject accidental CI execution, remote Redis, and repository write credentials."""

    if os.environ.get("AIR_RUN_LIVE_WORKFLOW") != "1":
        raise ValueError("explicit AIR_RUN_LIVE_WORKFLOW=1 opt-in is required")
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("HF_TOKEN"):
        raise ValueError("repository and alternative-provider credentials must be empty")
    redis_url = os.environ.get("AIR_INTEGRATION_REDIS_URL", "")
    parsed = urlparse(redis_url)
    if parsed.scheme != "redis" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("only a disposable loopback Redis service is allowed")
    if phase == "run" and not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("GEMINI_API_KEY is required for the explicitly opted-in live phase")
    if phase == "recover" and os.environ.get("GEMINI_API_KEY"):
        raise ValueError("recovery must run without a Gemini credential")
    return redis_url


def save_report(report: dict[str, Any], directory: Path) -> None:
    """Keep synthetic output on the ephemeral runner, without artifact uploads."""

    serialized = json.dumps(report, indent=2)
    key = os.environ.get("GEMINI_API_KEY", "")
    if key and key in serialized:
        raise ValueError("credential-bearing output withheld")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("run", "recover"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, force=True)
    try:
        redis_url = require_opt_in(args.phase)
        checkpointer = create_redis_checkpointer(redis_url)
        if args.phase == "run":
            settings = Settings(
                _env_file=None,
                model_provider="gemini",
                model_name="gemini-3.8-flash",
                model_reasoning_effort="low",
                model_max_output_tokens=1200,
                gemini_api_key=os.environ["GEMINI_API_KEY"],
                hf_token=None,
                github_token=None,
            )
            with GeminiStructuredModel.from_settings(settings) as model:
                report = run_cases(model, DockerSandboxRunner(), checkpointer)
        else:
            report = json.loads((args.output_dir / "report.json").read_text(encoding="utf-8"))
            report = recover_cases(report, DockerSandboxRunner(), checkpointer)
        save_report(report, args.output_dir)
        return 0 if report["passed"] else 1
    except Exception as error:
        # Never print arbitrary exception bodies, HTTP headers, environment, or tracebacks.
        print(f"Combined live verification stopped safely ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
