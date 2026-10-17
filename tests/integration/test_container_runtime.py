"""Opt-in integration checks using real validator images and a disposable Redis service."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from agentic_incident_remediation.models import ValidationStatus
from agentic_incident_remediation.sandbox import (
    DockerSandboxRunner,
    SandboxRequest,
    ValidationTool,
    normalize_validation_result,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AIR_RUN_CONTAINER_TESTS") != "1",
        reason="Set AIR_RUN_CONTAINER_TESTS=1 only with real validator images and test Redis",
    ),
]
DIRECTORY = Path(__file__).parent


@pytest.mark.parametrize(
    ("filename", "tool"),
    [
        ("deployment.yaml", ValidationTool.KUBECONFORM),
        ("security-group.tf", ValidationTool.TERRAFORM_VALIDATE),
    ],
)
@pytest.mark.parametrize("valid", [True, False])
def test_real_validators_accept_valid_and_reject_invalid_artifacts(
    filename: str, tool: ValidationTool, valid: bool
) -> None:
    content = (DIRECTORY / "fixtures" / filename).read_text()
    if not valid:
        content = (
            content.replace("replicas: 1", "replicas: not-an-integer")
            if tool is ValidationTool.KUBECONFORM
            else content.replace("  type", "  unsupported_attribute = true\n  type", 1)
        )
    request = SandboxRequest(artifact_id=uuid4(), tool=tool, content=content)

    execution = DockerSandboxRunner().run(request)
    result = normalize_validation_result(request.artifact_id, execution)

    expected = ValidationStatus.PASSED if valid else ValidationStatus.FAILED
    assert result.status is expected, result.model_dump_json()
    assert execution.timed_out is False
    if not valid:
        assert result.findings, "Invalid artifacts must produce structured findings"


def worker(mode: str, *arguments: str) -> dict[str, Any]:
    redis_url = os.environ["AIR_INTEGRATION_REDIS_URL"]
    parsed = urlparse(redis_url)
    assert parsed.scheme == "redis" and parsed.hostname in {"localhost", "127.0.0.1"}, (
        "Use only the disposable, loopback-bound integration Redis service"
    )
    environment = {**os.environ, "HF_TOKEN": "", "GITHUB_TOKEN": ""}
    result = subprocess.run(
        [sys.executable, str(DIRECTORY / "checkpoint_worker.py"), mode, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload: dict[str, Any] = json.loads(result.stdout.splitlines()[-1])
    return payload


@pytest.mark.parametrize("kind", ["kubernetes_oom_killed", "terraform_drift"])
def test_api_approval_checkpoint_survives_a_fresh_process(kind: str) -> None:
    pending = worker("start", "--kind", kind)
    assert pending["status"] == "awaiting_approval", pending
    assert pending["review_ready"] is True
    assert pending["validation_results"][-1]["status"] == "passed"
    assert pending["pull_request"] is None

    fresh = worker("recover", "--execution-id", pending["execution_id"])

    assert fresh["recovered"] == pending
    assert fresh["rejected"]["status"] == "rejected"
    assert fresh["rejected"]["pull_request"] is None
    assert fresh["rejected"]["approval"]["id"] == pending["approval"]["id"]


def test_paused_redis_checkpoint_resumes_and_terminates_in_a_fresh_process() -> None:
    paused = worker("pause")
    assert paused["control_status"] == "paused"
    assert paused["artifacts"] == []

    fresh = worker("resume", "--execution-id", paused["execution_id"])

    assert fresh["review"]["status"] == "awaiting_approval", fresh
    assert fresh["review"]["review_ready"] is True
    assert fresh["review"]["execution_id"] == paused["execution_id"]
    assert fresh["terminated"]["status"] == "terminated"
    assert fresh["terminated"]["review_ready"] is False
    assert fresh["terminated"]["pull_request"] is None
