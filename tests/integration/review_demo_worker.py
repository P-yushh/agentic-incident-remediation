"""Separate-process review continuity check; real Docker/Redis, no models or GitHub writes."""

import argparse
import json
import os
from pathlib import Path

from agentic_incident_remediation.demo import FixtureStructuredModel
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.operations import FakePullRequestPublisher
from agentic_incident_remediation.persistence import create_redis_checkpointer
from agentic_incident_remediation.review_demo import (
    PublicationDisabled,
    prepare_review,
    publish_review,
)
from agentic_incident_remediation.review_scenarios import DemoScenario
from agentic_incident_remediation.sandbox import DockerSandboxRunner, FakeSandboxRunner


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "publish"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--scenario", choices=[s.value for s in DemoScenario], default=DemoScenario.OOM.value
    )
    args = parser.parse_args()
    assert os.environ.get("AIR_RUN_CONTAINER_TESTS") == "1"
    assert not any(os.environ.get(key) for key in ("GITHUB_TOKEN", "GEMINI_API_KEY", "HF_TOKEN"))
    preparing = args.mode == "prepare"
    budget = BudgetedModel(FixtureStructuredModel())
    publisher = FakePullRequestPublisher()
    workflow = DurableApprovalWorkflow(
        budget if preparing else FakeStructuredModel([]),
        DockerSandboxRunner() if preparing else FakeSandboxRunner([]),
        PublicationDisabled() if preparing else publisher,
        repository="example/synthetic-integration",
        checkpointer=create_redis_checkpointer(os.environ["AIR_INTEGRATION_REDIS_URL"]),
        max_retries=1,
    )
    if preparing:
        manifest = prepare_review(
            workflow, budget, {"kind": "offline-container-integration"}, DemoScenario(args.scenario)
        )
        args.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        assert not publisher.proposals
    else:
        manifest = json.loads(args.manifest.read_text())
        result = publish_review(workflow, manifest, reviewer="synthetic-integration-reviewer")
        assert len(publisher.proposals) == 1
        assert result["execution_id"] == manifest["state"]["execution_id"]
        assert result["artifact_sha256"] == manifest["artifact_sha256"]
        assert result["publication_model_calls"] == 0
        print(json.dumps(result))


if __name__ == "__main__":
    main()
