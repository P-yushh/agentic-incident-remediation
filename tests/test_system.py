"""Credential-free system scenarios through the real HTTP and LangGraph boundaries."""

from fastapi.testclient import TestClient

from agentic_incident_remediation.demo import create_demo_app


def kubernetes_incident() -> dict[str, object]:
    return {
        "type": "kubernetes_oom_killed",
        "title": "Payment API pod exceeded its memory limit",
        "description": "The container restarted after an OOM event.",
        "severity": "high",
        "source": "system-test",
    }


def terraform_incident() -> dict[str, object]:
    return {
        "type": "invalid_security_group_rule",
        "title": "Service security group permits unrestricted ingress",
        "description": "Port 443 is currently open to every IPv4 address.",
        "severity": "critical",
        "source": "system-test",
    }


def decide(client: TestClient, pending: dict[str, object], decision: str) -> dict[str, object]:
    approval = pending["approval"]
    assert isinstance(approval, dict)
    response = client.post(
        f"/api/executions/{pending['execution_id']}/approval",
        json={
            "approval_id": approval["id"],
            "decision": decision,
            "decided_by": "system-test-reviewer",
            "comment": "Reviewed deterministic validation evidence.",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_kubernetes_incident_reaches_review_and_approved_pull_request() -> None:
    client = TestClient(create_demo_app())

    response = client.post("/api/incidents", json=kubernetes_incident())

    assert response.status_code == 202
    pending = response.json()
    assert pending["status"] == "awaiting_approval"
    assert pending["artifacts"][-1]["path"] == "remediations/payment-api-memory.yaml"
    assert pending["validation_results"][-1]["status"] == "passed"

    completed = decide(client, pending, "approved")

    assert completed["status"] == "completed"
    assert completed["pull_request"]["url"].endswith("/pull/42")
    assert "apply" not in completed["artifacts"][-1]["content"].lower()


def test_terraform_incident_can_be_rejected_without_repository_change() -> None:
    client = TestClient(create_demo_app())

    pending = client.post("/api/incidents", json=terraform_incident()).json()

    assert pending["status"] == "awaiting_approval"
    assert pending["artifacts"][-1]["path"].endswith(".tf")
    assert pending["validation_results"][-1]["tool"] == "terraform_validate"

    rejected = decide(client, pending, "rejected")

    assert rejected["status"] == "rejected"
    assert rejected["pull_request"] is None


def test_demo_runtime_handles_multiple_independent_executions() -> None:
    client = TestClient(create_demo_app())

    first = client.post("/api/incidents", json=kubernetes_incident()).json()
    second = client.post("/api/incidents", json=terraform_incident()).json()

    assert first["execution_id"] != second["execution_id"]
    assert client.get(f"/api/executions/{first['execution_id']}").json()["status"] == (
        "awaiting_approval"
    )
    assert client.get(f"/api/executions/{second['execution_id']}").json()["status"] == (
        "awaiting_approval"
    )
