"""HTTP boundary, event stream, and observability tests."""

import json
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import Incident, IncidentType
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.sandbox import (
    FakeSandboxRunner,
    SandboxExecution,
    ValidationTool,
)

REPOSITORY = "P-yushh/agentic-incident-remediation"
INCIDENT_ID = UUID("b74e09f5-3fca-4f77-8a58-b066f44949a4")


def incident_payload() -> dict[str, object]:
    return {
        "id": str(INCIDENT_ID),
        "type": "kubernetes_oom_killed",
        "title": "Payment API exceeded its memory limit",
        "description": "The payment-api container restarted after an OOM event.",
        "severity": "high",
        "source": "api-test",
    }


def make_plan(incident_id: str) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "summary": "Increase the container memory limit and request review.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate",
                "description": "Generate a Kubernetes proposal.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate",
                "description": "Validate the proposal in isolation.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Review",
                "description": "Pause for human review.",
            },
        ],
    }


def artifact_payload() -> dict[str, str]:
    return {
        "kind": "kubernetes_manifest",
        "path": "remediations/payment-api.yaml",
        "content": """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-api
spec:
  selector:
    matchLabels:
      app: payment-api
  template:
    metadata:
      labels:
        app: payment-api
    spec:
      containers:
        - name: api
          image: example/payment-api:1.0
          resources:
            limits:
              memory: 640Mi
""",
        "change_summary": "Increase the payment API memory limit.",
    }


def make_client() -> tuple[TestClient, FakePullRequestPublisher]:
    raw = incident_payload()
    incident = Incident(
        id=INCIDENT_ID,
        type=IncidentType.KUBERNETES_OOM_KILLED,
        title=str(raw["title"]),
        description=str(raw["description"]),
        severity=str(raw["severity"]),
        source=str(raw["source"]),
    )
    publisher = FakePullRequestPublisher()
    workflow = DurableApprovalWorkflow(
        FakeStructuredModel([make_plan(str(incident.id)), artifact_payload()]),
        FakeSandboxRunner(
            [
                SandboxExecution(
                    tool=ValidationTool.KUBECONFORM,
                    exit_code=0,
                    stdout=json.dumps({"resources": [], "summary": {"valid": 1}}),
                    duration_ms=4,
                )
            ]
        ),
        publisher,
        repository=REPOSITORY,
        checkpointer=InMemorySaver(),
    )

    client = TestClient(create_app(RemediationService(workflow)))
    return client, publisher


def test_api_exposes_dashboard_health_and_openapi() -> None:
    client, _ = make_client()

    dashboard = client.get("/")
    health = client.get("/health", headers={"x-request-id": "request-123"})
    schema = client.get("/openapi.json").json()

    assert dashboard.status_code == 200
    assert "Incident Remediation Console" in dashboard.text
    assert "Nothing is applied to infrastructure" in dashboard.text
    assert health.json() == {"status": "ok", "service": "agentic-incident-remediation"}
    assert health.headers["x-request-id"] == "request-123"
    assert "/api/incidents" in schema["paths"]


def test_dashboard_uses_sand_teal_palette_with_readable_text() -> None:
    client, _ = make_client()
    dashboard = client.get("/").text
    colors = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6});", dashboard))

    assert 'data-theme="sand-teal"' in dashboard
    assert "color-scheme: light;" in dashboard
    assert colors["bg"] == "#f7f4ee"
    assert colors["panel"] == "#fffdf8"
    assert colors["accent"] == "#096b66"

    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return sum(
            value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    for foreground, background in (
        ("ink", "panel"),
        ("ink", "soft"),
        ("ink", "side"),
        ("muted", "bg"),
        ("muted", "panel"),
        ("muted", "accent-bg"),
        ("accent", "accent-bg"),
        ("on-accent", "accent"),
        ("danger", "panel"),
        ("on-danger", "danger"),
    ):
        lighter, darker = sorted(
            (luminance(colors[foreground]), luminance(colors[background])), reverse=True
        )
        assert (lighter + 0.05) / (darker + 0.05) >= 4.5, (foreground, background)


def test_approved_api_flow_streams_state_and_records_metrics() -> None:
    client, publisher = make_client()

    started = client.post("/api/incidents", json=incident_payload())

    assert started.status_code == 202
    pending = started.json()
    assert pending["status"] == "awaiting_approval"
    execution_id = pending["execution_id"]
    approval_id = pending["approval"]["id"]

    restored = client.get(f"/api/executions/{execution_id}")
    events = client.get(f"/api/executions/{execution_id}/events?follow=false")
    approved = client.post(
        f"/api/executions/{execution_id}/approval",
        json={
            "approval_id": approval_id,
            "decision": "approved",
            "decided_by": "reviewer@example.com",
            "comment": "Sandbox result reviewed.",
        },
    )
    metrics = client.get("/metrics")

    assert restored.json()["status"] == "awaiting_approval"
    assert events.status_code == 200
    assert "event: state" in events.text
    assert '"status":"awaiting_approval"' in events.text
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert approved.json()["pull_request"]["url"].endswith("/pull/42")
    assert len(publisher.proposals) == 1
    assert "remediation_executions_started_total 1.0" in metrics.text
    assert 'remediation_approval_decisions_total{decision="approved"} 1.0' in metrics.text
    assert 'remediation_workflow_outcomes_total{status="completed"} 1.0' in metrics.text


def test_api_rejects_stale_or_unknown_execution_requests() -> None:
    client, publisher = make_client()
    missing = client.get(f"/api/executions/{uuid4()}")
    pending = client.post("/api/incidents", json=incident_payload()).json()

    mismatch = client.post(
        f"/api/executions/{pending['execution_id']}/approval",
        json={
            "approval_id": str(uuid4()),
            "decision": "rejected",
            "decided_by": "reviewer@example.com",
        },
    )

    assert missing.status_code == 404
    assert mismatch.status_code == 409
    assert publisher.proposals == []


def test_api_validates_incidents_and_final_approval_decisions() -> None:
    client, _ = make_client()
    invalid_incident = client.post(
        "/api/incidents",
        json={"type": "unknown", "title": "", "description": ""},
    )

    pending = client.post("/api/incidents", json=incident_payload()).json()
    invalid_decision = client.post(
        f"/api/executions/{pending['execution_id']}/approval",
        json={
            "approval_id": pending["approval"]["id"],
            "decision": "pending",
            "decided_by": "reviewer@example.com",
        },
    )

    assert invalid_incident.status_code == 422
    assert invalid_decision.status_code == 409
