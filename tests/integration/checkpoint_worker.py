"""Separate-process HTTP workflow exercise used only by container integration tests."""

import argparse
import json
import os
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.demo import FixtureStructuredModel
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.persistence import create_redis_checkpointer
from agentic_incident_remediation.sandbox import DockerSandboxRunner
from agentic_incident_remediation.schema_tools import LocalSchemaToolProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["start", "recover", "pause", "resume"])
    parser.add_argument("--kind", choices=["kubernetes_oom_killed", "terraform_drift"])
    parser.add_argument("--execution-id", type=UUID)
    args = parser.parse_args()
    publisher = FakePullRequestPublisher()
    model = FakeStructuredModel([]) if args.mode == "recover" else FixtureStructuredModel()
    checkpointer = create_redis_checkpointer(os.environ["AIR_INTEGRATION_REDIS_URL"])
    workflow = DurableApprovalWorkflow(
        model,
        DockerSandboxRunner(),
        publisher,
        repository="example/synthetic-integration",
        schema_tools=LocalSchemaToolProvider(),
        checkpointer=checkpointer,
        max_retries=1,
    )
    service = RemediationService(workflow)
    with TestClient(create_app(service)) as client:
        if args.mode == "start":
            response = client.post(
                "/api/incidents",
                json={
                    "type": args.kind,
                    "title": "Synthetic container integration alert",
                    "description": "Validate a proposal without applying infrastructure changes.",
                    "source": "container-integration",
                },
            )
            assert response.status_code == 202, response.text
            result = response.json()
            if not result["review_ready"]:
                config = workflow._config(UUID(result["execution_id"]))
                snapshot = workflow._graph.get_state(config)
                checkpoint = checkpointer.get_tuple(config)
                result["checkpoint_diagnostics"] = {
                    "next": snapshot.next,
                    "tasks": [
                        {"id": task.id, "name": task.name, "interrupts": len(task.interrupts)}
                        for task in snapshot.tasks
                    ],
                    "pending_writes": [
                        {"task": task, "channel": channel, "value_type": type(value).__name__}
                        for task, channel, value in (checkpoint.pending_writes or [])
                    ]
                    if checkpoint is not None
                    else [],
                }
        elif args.mode == "pause":
            state = workflow.prepare(
                {
                    "type": "kubernetes_oom_killed",
                    "title": "Synthetic pause recovery",
                    "description": "Pause before planning and restore in another process.",
                }
            )
            response = client.post(
                f"/api/executions/{state.execution_id}/control", json={"action": "pause"}
            )
            assert response.status_code == 200, response.text
            result = response.json()
        else:
            assert args.execution_id is not None
            base = f"/api/executions/{args.execution_id}"
            response = client.get(base)
            assert response.status_code == 200, response.text
            recovered = response.json()
            if args.mode == "recover":
                stale = client.post(
                    base + "/approval",
                    json={
                        "approval_id": str(uuid4()),
                        "decision": "rejected",
                        "decided_by": "integration-reviewer",
                    },
                )
                assert stale.status_code == 409, stale.text
                rejected = client.post(
                    base + "/approval",
                    json={
                        "approval_id": recovered["approval"]["id"],
                        "decision": "rejected",
                        "decided_by": "integration-reviewer",
                    },
                )
                assert rejected.status_code == 200, rejected.text
                result = {"recovered": recovered, "rejected": rejected.json()}
            else:
                assert recovered["control_status"] == "paused"
                resumed = client.post(base + "/control", json={"action": "resume"})
                assert resumed.status_code == 200, resumed.text
                future = service._slots[args.execution_id].future
                assert future is not None
                future.result(timeout=90)
                review = client.get(base).json()
                terminated = client.post(base + "/control", json={"action": "terminate"})
                assert terminated.status_code == 200, terminated.text
                result = {"review": review, "terminated": terminated.json()}
        assert publisher.proposals == [], "Integration tests must never publish a pull request"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
