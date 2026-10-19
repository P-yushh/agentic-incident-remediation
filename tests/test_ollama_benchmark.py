"""Evaluation harness safety and independent scenario assertions, without inference."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("hcl2")
pytest.importorskip("yaml")

from agentic_incident_remediation.review_scenarios import DemoScenario
from agentic_incident_remediation.sandbox.models import (
    SandboxRequest,
    ValidationTool,
)

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "ollama_benchmark", ROOT / "scripts/benchmark_ollama.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def fixed_content(scenario):
    extension = "tf" if scenario is DemoScenario.TERRAFORM else "yaml"
    content = (
        ROOT
        / "src/agentic_incident_remediation/actions/baselines"
        / f"{scenario.value}.{extension}"
    ).read_text()
    if scenario is DemoScenario.OOM:
        return content.replace("256Mi", "512Mi").replace("128Mi", "256Mi")
    if scenario is DemoScenario.CRASH_LOOP:
        return content.replace("worker-config-old", "worker-config")
    return content.replace("0.0.0.0/0", "10.20.0.0/16")


@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_known_correct_fix_matches_and_baseline_does_not(scenario):
    assert benchmark.scenario_matches(scenario, fixed_content(scenario))
    wrong = (
        fixed_content(scenario)
        .replace("512Mi", "256Mi")
        .replace("name: worker-config\n", "name: worker-config-old\n")
        .replace("10.20.0.0/16", "0.0.0.0/0")
    )
    assert not benchmark.scenario_matches(scenario, wrong)


@pytest.mark.parametrize("content", ["", "[", "null", "a: b", "---\n{}\n---\n{}", "x"])
@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_malformed_or_unrelated_output_rejected(scenario, content):
    assert not benchmark.scenario_matches(scenario, content)


@pytest.mark.parametrize(
    "extra",
    [
        '\nprovider "aws" {}',
        '\nterraform { backend "s3" {} }',
        '\ndata "external" "example" { program = ["bad"] }',
        '\nresource "null_resource" "extra" {}',
    ],
)
def test_extra_terraform_blocks_rejected_before_native_execution(extra):
    assert not benchmark.scenario_matches(
        DemoScenario.TERRAFORM, fixed_content(DemoScenario.TERRAFORM) + extra
    )


@pytest.mark.parametrize("scenario", [DemoScenario.OOM, DemoScenario.CRASH_LOOP])
def test_empty_yaml_document_is_not_an_extra_resource(scenario):
    assert benchmark.scenario_matches(scenario, fixed_content(scenario) + "---\n# End\n")
    assert not benchmark.scenario_matches(scenario, fixed_content(scenario) + "---\n{}\n")


def test_native_runner_has_no_inherited_credentials_or_apply(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-inherit")
    monkeypatch.setenv("GOOGLE_API_KEY", "do-not-inherit")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
        assert "GOOGLE_API_KEY" not in kwargs["env"]
        assert not kwargs.get("shell")
        assert not any(word in argv for word in ["apply", "destroy", "plan"])
        return subprocess.CompletedProcess(argv, 0, '{"valid":true}', "")

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    result = benchmark.NativeBenchmarkRunner(tmp_path).run(
        SandboxRequest(
            artifact_id=uuid4(),
            tool=ValidationTool.TERRAFORM_VALIDATE,
            content=fixed_content(DemoScenario.TERRAFORM),
        )
    )
    assert result.exit_code == 0
    assert len(calls) == 2
    assert "-backend=false" in calls[0][0]
    assert "-plugin-dir=" in calls[0][0][-1]
    assert calls[1][0][-2:] == ["validate", "-json"]


@pytest.mark.parametrize("failure", [OSError("not executable"), subprocess.TimeoutExpired("x", 30)])
def test_native_tool_failure_is_recorded(monkeypatch, tmp_path, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    result = benchmark.NativeBenchmarkRunner(tmp_path).run(
        SandboxRequest(
            artifact_id=uuid4(),
            tool=ValidationTool.KUBECONFORM,
            content=fixed_content(DemoScenario.OOM),
        )
    )
    assert result.exit_code != 0
    assert result.timed_out == isinstance(failure, subprocess.TimeoutExpired)


def passing_records():
    return [
        {"scenario": scenario.value, "repeat": repeat, "passed": True, "generation_seconds": 30}
        for scenario in DemoScenario
        for repeat in range(1, 4)
    ]


def test_gate_rejects_partial_duplicate_failed_or_slow_trials():
    records = passing_records()
    assert benchmark.meets_gate(records)
    assert not benchmark.meets_gate(records[:8])
    assert not benchmark.meets_gate([records[0]] * 9)
    records[0]["passed"] = False
    assert not benchmark.meets_gate(records)
    records[0]["passed"] = True
    records[0]["generation_seconds"] = 90
    assert not benchmark.meets_gate(records)
    for record in records:
        record["generation_seconds"] = 60
    assert not benchmark.meets_gate(records)


def test_recheck_preserves_original_scores_and_never_calls_model(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("recheck must not make inference calls")

    monkeypatch.setattr(benchmark, "OllamaStructuredModel", forbidden)
    monkeypatch.setattr(
        benchmark.NativeBenchmarkRunner,
        "run",
        lambda self, request: benchmark.SandboxExecution(
            tool=request.tool, exit_code=0, duration_ms=1
        ),
    )
    content = fixed_content(DemoScenario.OOM) + "---\n# End\n"
    (tmp_path / "fix.yaml").write_text(content)
    trial = passing_records()[0] | {
        "passed": False,
        "failure": "old checker",
        "artifact": "fix.yaml",
        "artifact_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    original = json.dumps({"trials": [trial]}).encode()
    (tmp_path / "report.json").write_bytes(original)
    report = benchmark.assess_existing(tmp_path, tmp_path)
    assert report["trials"][0]["passed"] is True
    assert report["trials"][0]["original_passed"] is False
    assert report["trials"][0]["original_failure"] == "old checker"
    assert not report["eligible_for_selectable_mode"]
    assert (tmp_path / "report.json").read_bytes() == original
    (tmp_path / "fix.yaml").write_text("tampered")
    with pytest.raises(ValueError, match="changed since inference"):
        benchmark.assess_existing(tmp_path, tmp_path)
