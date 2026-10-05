"""Isolated runner, output normalization, and validator workflow tests."""

import json
import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from agentic_incident_remediation.artifacts import materialize_artifact
from agentic_incident_remediation.graph import (
    SandboxedRemediationWorkflow,
    ValidatorRoute,
    build_validation_graph,
    route_after_validator,
)
from agentic_incident_remediation.llm import FakeStructuredModel
from agentic_incident_remediation.models import (
    AgentState,
    ArtifactDraft,
    ArtifactKind,
    Incident,
    IncidentType,
    RemediationPlan,
    ValidationStatus,
    WorkflowStatus,
)
from agentic_incident_remediation.sandbox import (
    DockerSandboxRunner,
    FakeSandboxRunner,
    SandboxExecution,
    SandboxRequest,
    SandboxUnavailableError,
    ValidationTool,
    ValidatorNode,
    normalize_validation_result,
    validation_tools_for_artifact,
)


def make_incident(incident_type: IncidentType = IncidentType.KUBERNETES_OOM_KILLED) -> Incident:
    return Incident(
        type=incident_type,
        title="Synthetic validation incident",
        description="A generated remediation must be validated in isolation.",
        source="test-fixture",
    )


def make_plan_payload(incident: Incident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "summary": "Generate and validate a safe configuration change.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate a proposal",
                "description": "Create one declarative artifact.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate in isolation",
                "description": "Run an allowlisted validator in a locked-down container.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Request review",
                "description": "Require human approval before repository operations.",
            },
        ],
    }


def kubernetes_artifact_payload() -> dict[str, str]:
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
              memory: 512Mi
""",
        "change_summary": "Increase the container memory limit.",
    }


def validating_state() -> AgentState:
    incident = make_incident()
    artifact = materialize_artifact(ArtifactDraft.model_validate(kubernetes_artifact_payload()))
    return AgentState(
        incident=incident,
        status=WorkflowStatus.VALIDATING,
        plan=RemediationPlan.model_validate(make_plan_payload(incident)),
        artifacts=(artifact,),
    )


def kube_execution(exit_code: int = 0, *, invalid: bool = False) -> SandboxExecution:
    payload = {
        "resources": (
            [
                {
                    "filename": "stdin",
                    "kind": "Deployment",
                    "version": "apps/v1",
                    "status": "INVALID",
                    "msg": "Additional property resourcez is not allowed",
                }
            ]
            if invalid
            else []
        ),
        "summary": {"valid": 0 if invalid else 1, "invalid": 1 if invalid else 0},
    }
    return SandboxExecution(
        tool=ValidationTool.KUBECONFORM,
        exit_code=exit_code,
        stdout=json.dumps(payload),
        duration_ms=12,
    )


def test_docker_runner_builds_locked_down_command_without_artifact_arguments() -> None:
    captured: dict[str, object] = {}

    def execute(
        args: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            args=tuple(args),
            input=input,
            text=text,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
        )
        return subprocess.CompletedProcess(args, 0, stdout='{"resources": []}', stderr="")

    content = "apiVersion: v1\nkind: ConfigMap"
    request = SandboxRequest(
        artifact_id=validating_state().artifacts[-1].id,
        tool=ValidationTool.KUBECONFORM,
        content=content,
    )

    result = DockerSandboxRunner(executor=execute).run(request)

    command = captured["args"]
    assert isinstance(command, tuple)
    assert command[command.index("--network") :][:2] == ("--network", "none")
    assert "--read-only" in command
    assert command[command.index("--cap-drop") :][:2] == ("--cap-drop", "ALL")
    assert "no-new-privileges" in command
    assert "--pids-limit" in command
    assert "--memory" in command
    assert "--cpus" in command
    assert content not in command
    assert captured["input"] == content
    assert result.exit_code == 0


def test_docker_runner_normalizes_timeout_and_missing_runtime() -> None:
    def timeout_executor(
        args: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, timeout, output="partial output")

    def missing_executor(
        args: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    request = SandboxRequest(
        artifact_id=validating_state().artifacts[-1].id,
        tool=ValidationTool.KUBECONFORM,
        content="apiVersion: v1",
    )

    timeout = DockerSandboxRunner(executor=timeout_executor).run(request)
    assert timeout.timed_out is True
    assert timeout.exit_code == -1
    assert timeout.stdout == "partial output"
    with pytest.raises(SandboxUnavailableError, match="docker"):
        DockerSandboxRunner(executor=missing_executor).run(request)


@pytest.mark.parametrize(
    ("timeout", "pids", "message"),
    [(0.0, 64, "timeout"), (10.0, 0, "PID")],
)
def test_docker_runner_rejects_invalid_limits(timeout: float, pids: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DockerSandboxRunner(timeout_seconds=timeout, pids_limit=pids)


def test_kubeconform_failure_is_normalized_to_actionable_finding() -> None:
    artifact_id = validating_state().artifacts[-1].id

    result = normalize_validation_result(artifact_id, kube_execution(1, invalid=True))

    assert result.status is ValidationStatus.FAILED
    assert result.findings[0].code == "KUBECONFORM_INVALID"
    assert "resourcez" in result.findings[0].message
    assert result.findings[0].path == "stdin"


def test_terraform_json_diagnostics_preserve_source_location() -> None:
    artifact_id = validating_state().artifacts[-1].id
    execution = SandboxExecution(
        tool=ValidationTool.TERRAFORM_VALIDATE,
        exit_code=1,
        stdout=json.dumps(
            {
                "valid": False,
                "diagnostics": [
                    {
                        "severity": "error",
                        "summary": "Reference to undeclared input variable",
                        "detail": "Declare variable security_group_id.",
                        "range": {"filename": "main.tf", "start": {"line": 8}},
                    }
                ],
            }
        ),
        duration_ms=9,
    )

    result = normalize_validation_result(artifact_id, execution)

    assert result.status is ValidationStatus.FAILED
    assert result.findings[0].path == "main.tf"
    assert result.findings[0].line == 8
    assert "security_group_id" in result.findings[0].message


def test_timeout_and_runtime_exit_are_operational_errors() -> None:
    artifact_id = validating_state().artifacts[-1].id
    timeout = SandboxExecution(
        tool=ValidationTool.KUBECONFORM,
        exit_code=-1,
        stderr="sandbox validation timed out",
        duration_ms=20_000,
        timed_out=True,
    )
    runtime_error = SandboxExecution(
        tool=ValidationTool.KUBECONFORM,
        exit_code=125,
        stderr="docker daemon unavailable",
        duration_ms=1,
    )

    assert normalize_validation_result(artifact_id, timeout).status is ValidationStatus.ERROR
    assert normalize_validation_result(artifact_id, runtime_error).status is ValidationStatus.ERROR


def test_validator_routes_pass_failure_and_runner_error() -> None:
    state = validating_state()

    passed = AgentState.model_validate(
        state.model_dump() | ValidatorNode(FakeSandboxRunner([kube_execution()]))(state)
    )
    failed = AgentState.model_validate(
        state.model_dump()
        | ValidatorNode(FakeSandboxRunner([kube_execution(1, invalid=True)]))(state)
    )
    errored = AgentState.model_validate(
        state.model_dump()
        | ValidatorNode(FakeSandboxRunner([SandboxUnavailableError("docker unavailable")]))(state)
    )

    assert passed.status is WorkflowStatus.AWAITING_APPROVAL
    assert route_after_validator(passed) is ValidatorRoute.APPROVAL_READY
    assert failed.status is WorkflowStatus.REFLECTING
    assert route_after_validator(failed) is ValidatorRoute.REFLECTION_READY
    assert "resourcez" in failed.validation_errors[0]
    assert errored.status is WorkflowStatus.FAILED
    assert route_after_validator(errored) is ValidatorRoute.FAILED
    assert errored.failure_reason == "sandbox validation could not complete safely"


def test_sandboxed_workflow_validates_generated_artifact_end_to_end() -> None:
    incident = make_incident()
    model = FakeStructuredModel([make_plan_payload(incident), kubernetes_artifact_payload()])
    runner = FakeSandboxRunner([kube_execution()])

    result = SandboxedRemediationWorkflow(model, runner).invoke(incident)

    assert result.status is WorkflowStatus.AWAITING_APPROVAL
    assert result.validation_results[-1].status is ValidationStatus.PASSED
    assert runner.requests[0].content == result.artifacts[-1].content
    assert result.messages[-1].name == "sandbox-validator"


def test_validation_graph_exposes_validator_stage_and_routes() -> None:
    graph = build_validation_graph(FakeStructuredModel([]), FakeSandboxRunner([]))
    drawable = graph.get_graph()

    assert {"prepare_planning", "planner", "generator", "validator"}.issubset(drawable.nodes)
    assert any(edge.source == "generator" and edge.target == "validator" for edge in drawable.edges)
    with pytest.raises(ValueError, match="requires an artifact"):
        route_after_validator(AgentState(incident=make_incident()))


def test_tool_selection_matches_artifact_kind() -> None:
    assert validation_tools_for_artifact(ArtifactKind.KUBERNETES_MANIFEST) == (
        ValidationTool.KUBECONFORM,
    )
    assert validation_tools_for_artifact(ArtifactKind.TERRAFORM_CONFIGURATION) == (
        ValidationTool.TERRAFORM_VALIDATE,
    )
