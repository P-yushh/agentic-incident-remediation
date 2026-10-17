"""Deterministic, credential-free demonstration runtime."""

import json
import re
from typing import TypeVar
from uuid import UUID

import uvicorn
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from agentic_incident_remediation.api.app import create_app
from agentic_incident_remediation.api.service import RemediationService
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import ModelGatewayError, ModelRequest
from agentic_incident_remediation.models import ArtifactDraft, RemediationPlan
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.sandbox import (
    SandboxExecution,
    SandboxRequest,
    ValidationTool,
)

StructuredResponse = TypeVar("StructuredResponse", bound=BaseModel)
REPOSITORY = "P-yushh/agentic-incident-remediation"
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


class FixtureStructuredModel:
    """Generate stable typed responses from the request context without an API call."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        self.requests.append(
            ModelRequest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=response_model.__name__,
            )
        )
        if response_model is RemediationPlan:
            payload = _plan_fixture(user_prompt)
        elif response_model is ArtifactDraft:
            payload = _artifact_fixture(user_prompt)
        else:
            raise ModelGatewayError(f"no fixture exists for {response_model.__name__}")
        return response_model.model_validate(payload)


class FixtureSandboxRunner:
    """Return passing validation output for the workflow's fixed validator allowlist."""

    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxExecution:
        self.requests.append(request)
        stdout = (
            json.dumps({"resources": [], "summary": {"valid": 1, "invalid": 0}})
            if request.tool is ValidationTool.KUBECONFORM
            else json.dumps({"valid": True, "diagnostics": []})
        )
        return SandboxExecution(
            tool=request.tool,
            exit_code=0,
            stdout=stdout,
            duration_ms=3,
        )


def create_demo_service() -> RemediationService:
    """Build a repeatable in-memory workflow with no credentials or external services."""

    workflow = DurableApprovalWorkflow(
        FixtureStructuredModel(),
        FixtureSandboxRunner(),
        FakePullRequestPublisher(),
        repository=REPOSITORY,
        schema_tools=None,
        checkpointer=InMemorySaver(),
    )
    return RemediationService(workflow, step_delay_seconds=0.75)


def create_demo_app() -> FastAPI:
    """Return the credential-free FastAPI demonstration application."""

    return create_app(create_demo_service())


def main() -> None:
    """Run the offline demonstration console."""

    uvicorn.run(create_demo_app(), host="127.0.0.1", port=8000)


def _plan_fixture(user_prompt: str) -> dict[str, object]:
    match = _UUID_PATTERN.search(user_prompt)
    if match is None:
        raise ModelGatewayError("fixture planner could not find the incident UUID")
    incident_id = UUID(match.group())
    return {
        "incident_id": str(incident_id),
        "summary": "Prepare the smallest declarative correction and request human review.",
        "steps": [
            {
                "order": 1,
                "kind": "fetch_schema",
                "title": "Inspect resource contract",
                "description": "Load the approved resource schema snapshot.",
            },
            {
                "order": 2,
                "kind": "generate",
                "title": "Generate proposal",
                "description": "Generate one repository-confined declarative change.",
            },
            {
                "order": 3,
                "kind": "validate",
                "title": "Validate safely",
                "description": "Validate the proposal in the isolated sandbox.",
            },
            {
                "order": 4,
                "kind": "request_approval",
                "title": "Request review",
                "description": "Pause until a human approves or rejects the proposal.",
            },
        ],
        "assumptions": ["The checked-in fixture represents the current resource shape."],
        "risk_notes": ["This demonstration creates a proposal and never applies it."],
    }


def _artifact_fixture(user_prompt: str) -> dict[str, object]:
    if "terraform_configuration" in user_prompt:
        return {
            "kind": "terraform_configuration",
            "path": "remediations/restrict-security-group.tf",
            "content": (
                'resource "aws_security_group_rule" "service_https" {\n'
                '  type              = "ingress"\n'
                "  from_port         = 443\n"
                "  to_port           = 443\n"
                '  protocol          = "tcp"\n'
                '  cidr_blocks       = ["10.20.0.0/16"]\n'
                '  security_group_id = "sg-demo123"\n'
                "}\n"
            ),
            "change_summary": "Restrict service ingress to the private application network.",
        }
    return {
        "kind": "kubernetes_manifest",
        "path": "remediations/payment-api-memory.yaml",
        "content": (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: payment-api\n"
            "spec:\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: payment-api\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: payment-api\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: api\n"
            "          image: example/payment-api:1.0\n"
            "          resources:\n"
            "            requests:\n"
            "              memory: 384Mi\n"
            "            limits:\n"
            "              memory: 640Mi\n"
        ),
        "change_summary": "Raise the payment API memory request and limit.",
    }
