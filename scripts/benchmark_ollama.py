"""Bounded local-model evaluation; never publish or apply.

Install the optional benchmark extra, then prepare temporary native tools with
scripts/prepare_benchmark_tools.py --directory <private-directory>/tools.
Run this script with --tools <private-directory>/tools --output <fresh-report-directory>.
The default is three trials per scenario, at most two local calls each, no repairs.
--recheck revalidates existing saved outputs without contacting any model.

Acceptance requires all nine trials passing, median generation <60s and p95 <90s.
Native validators do not provide the isolation of the application's Docker sandbox.
Fixed scenarios supply the desired change; this is not a general diagnostic evaluation.
"""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import hcl2
import httpx
import yaml
from lark.exceptions import LarkError

from agentic_incident_remediation.generator.node import GeneratorNode
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.llm.ollama import OllamaStructuredModel
from agentic_incident_remediation.models import AgentState, Incident, WorkflowStatus
from agentic_incident_remediation.planner.node import PlannerNode
from agentic_incident_remediation.review_scenarios import SCENARIOS, DemoScenario
from agentic_incident_remediation.sandbox.models import (
    SandboxExecution,
    SandboxRequest,
    ValidationTool,
)
from agentic_incident_remediation.sandbox.node import ValidatorNode


def scenario_matches(scenario: DemoScenario, content: str) -> bool:
    """Independent assertions for the actual requested change, beyond syntactic validity."""
    try:
        if scenario is DemoScenario.TERRAFORM:
            document = hcl2.loads(
                content,
                serialization_options=hcl2.utils.SerializationOptions(
                    with_comments=False, explicit_blocks=False, strip_string_quotes=True
                ),
            )
            if set(document) != {"resource"} or len(document["resource"]) != 1:
                return False
            resource = document["resource"][0]
            if set(resource) != {"aws_security_group_rule"} or set(
                resource["aws_security_group_rule"]
            ) != {"service_https"}:
                return False
            return resource["aws_security_group_rule"]["service_https"] == {
                "type": "ingress",
                "from_port": 443,
                "to_port": 443,
                "protocol": "tcp",
                "security_group_id": "sg-demo123",
                "cidr_blocks": ["10.20.0.0/16"],
            }
        # YAML document separators can leave empty documents; kubeconform ignores these too.
        documents = [document for document in yaml.safe_load_all(content) if document is not None]
        if len(documents) != 1:
            return False
        document = documents[0]
        service = SCENARIOS[scenario].service
        if document["apiVersion"] != "apps/v1" or document["kind"] != "Deployment":
            return False
        metadata, spec = document["metadata"], document["spec"]
        if metadata["name"] != service or metadata["namespace"] != "demo" or spec["replicas"] != 1:
            return False
        if spec["selector"] != {"matchLabels": {"app": service}} or spec["template"]["metadata"][
            "labels"
        ] != {"app": service}:
            return False
        pod = spec["template"]["spec"]
        if set(pod) != {"containers"} or len(pod["containers"]) != 1:
            return False
        container = pod["containers"][0]
        if container["image"] != f"example/{service}:1.0":
            return False
        if scenario is DemoScenario.OOM:
            return (
                container["name"] == "api"
                and set(container) == {"name", "image", "resources"}
                and container["resources"]
                == {"requests": {"memory": "256Mi"}, "limits": {"memory": "512Mi"}}
            )
        return (
            container["name"] == "worker"
            and set(container) == {"name", "image", "envFrom"}
            and container["envFrom"] == [{"configMapRef": {"name": "worker-config"}}]
        )
    except (ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError, LarkError):
        return False


class NativeBenchmarkRunner:
    """Read-only native checks, explicitly NOT the Docker sandbox used by Actions."""

    def __init__(self, tools: Path) -> None:
        self.tools = tools.resolve()

    def run(self, request: SandboxRequest) -> SandboxExecution:
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="air-native-validation-") as path:
            directory = Path(path)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": path,
                "TMPDIR": path,
                "TF_DATA_DIR": str(directory / ".terraform"),
                "TF_IN_AUTOMATION": "1",
                "CHECKPOINT_DISABLE": "1",
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_CONFIG_FILE": str(directory / "no-aws-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(directory / "no-aws-credentials"),
            }
            try:
                if request.tool is ValidationTool.KUBECONFORM:
                    result = subprocess.run(
                        [
                            str(self.tools / "kubeconform"),
                            "-strict",
                            "-summary",
                            "-output",
                            "json",
                            "-kubernetes-version",
                            "1.31.0",
                            "-schema-location",
                            f"file://{self.tools}/{{{{.ResourceKind}}}}{{{{.KindSuffix}}}}.json",
                            "-",
                        ],
                        input=request.content,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        cwd=directory,
                        env=env,
                        check=False,
                    )
                else:
                    (directory / "main.tf").write_text(request.content)
                    (directory / "versions.tf").write_text(
                        "terraform {\n required_providers {\n"
                        ' aws = { source = "hashicorp/aws", version = "=6.2.0" }\n }\n}\n'
                    )
                    init = subprocess.run(
                        [
                            str(self.tools / "terraform"),
                            "init",
                            "-backend=false",
                            "-input=false",
                            "-no-color",
                            f"-plugin-dir={self.tools / 'providers'}",
                        ],
                        text=True,
                        capture_output=True,
                        timeout=60,
                        cwd=directory,
                        env=env,
                        check=False,
                    )
                    if init.returncode:
                        return SandboxExecution(
                            tool=request.tool,
                            exit_code=125,
                            stderr=init.stderr[:20_000],
                            duration_ms=int((perf_counter() - started) * 1000),
                        )
                    result = subprocess.run(
                        [str(self.tools / "terraform"), "validate", "-json"],
                        text=True,
                        capture_output=True,
                        timeout=30,
                        cwd=directory,
                        env=env,
                        check=False,
                    )
            except subprocess.TimeoutExpired:
                return SandboxExecution(
                    tool=request.tool,
                    exit_code=-1,
                    timed_out=True,
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            except OSError:
                return SandboxExecution(
                    tool=request.tool,
                    exit_code=125,
                    stderr="Native validator could not be executed",
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            return SandboxExecution(
                tool=request.tool,
                exit_code=result.returncode,
                stdout=result.stdout[:20_000],
                stderr=result.stderr[:20_000],
                duration_ms=int((perf_counter() - started) * 1000),
            )


def run_trial(scenario: DemoScenario, repeat: int, tools: Path, output: Path) -> dict:
    started = perf_counter()
    record = {
        "scenario": scenario.value,
        "repeat": repeat,
        "passed": False,
        "semantic_match": False,
        "native_validation": "not_run",
        "failure": None,
    }
    state = AgentState(
        incident=Incident.model_validate(SCENARIOS[scenario].incident()),
        status=WorkflowStatus.PLANNING,
    )
    with OllamaStructuredModel(seed=repeat) as model:
        budget = BudgetedModel(model)
        state = state.model_copy(update=PlannerNode(budget)(state))
        if state.status is WorkflowStatus.GENERATING:
            state = state.model_copy(update=GeneratorNode(budget)(state))
        record["generation_seconds"] = round(perf_counter() - started, 3)
        record["model_calls"] = model.calls
        if state.status is WorkflowStatus.VALIDATING:
            artifact = state.artifacts[0]
            filename = f"{scenario.value}-{repeat}.{SCENARIOS[scenario].extension}"
            (output / filename).write_text(artifact.content)
            record["artifact"] = filename
            record["artifact_sha256"] = hashlib.sha256(artifact.content.encode()).hexdigest()
            record["semantic_match"] = scenario_matches(scenario, artifact.content)
            if record["semantic_match"]:
                state = state.model_copy(update=ValidatorNode(NativeBenchmarkRunner(tools))(state))
                record["native_validation"] = (
                    "passed" if state.status is WorkflowStatus.AWAITING_APPROVAL else "failed"
                )
                record["validation_results"] = [
                    r.model_dump(mode="json") for r in state.validation_results
                ]
                record["passed"] = state.status is WorkflowStatus.AWAITING_APPROVAL
            else:
                record["failure"] = "scenario or declarative-only safety assertions failed"
        else:
            record["failure"] = state.failure_reason
            (output / f"{scenario.value}-{repeat}-invalid-output.txt").write_text(
                model.last_content
            )
    record["total_seconds"] = round(perf_counter() - started, 3)
    return record


def meets_gate(records: list[dict]) -> bool:
    expected = {(scenario.value, repeat) for scenario in SCENARIOS for repeat in range(1, 4)}
    actual = {(record["scenario"], record["repeat"]) for record in records}
    if len(records) != 9 or actual != expected or not all(record["passed"] for record in records):
        return False
    elapsed = sorted(record["generation_seconds"] for record in records)
    return statistics.median(elapsed) < 60 and elapsed[math.ceil(0.95 * len(elapsed)) - 1] < 90


def assess_existing(tools: Path, output: Path) -> dict:
    """Recheck exact saved outputs without inference; preserve the original raw report."""
    raw = (output / "report.json").read_bytes()
    original = json.loads(raw)
    records = []
    runner = NativeBenchmarkRunner(tools)
    for trial in original["trials"]:
        result = dict(trial)
        result["original_passed"] = trial["passed"]
        result["original_failure"] = trial["failure"]
        result["passed"] = False
        if "artifact" in trial:
            filename = trial["artifact"]
            if Path(filename).name != filename:
                raise ValueError("saved artifact must be confined to the report directory")
            content = (output / filename).read_text()
            if hashlib.sha256(content.encode()).hexdigest() != trial["artifact_sha256"]:
                raise ValueError("saved artifact changed since inference")
            scenario = DemoScenario(trial["scenario"])
            result["semantic_match"] = scenario_matches(scenario, content)
            # kubeconform only reads YAML. Terraform can load plugins, so require the
            # exact declarative resource allowlist before invoking it natively.
            if result["semantic_match"] or scenario is not DemoScenario.TERRAFORM:
                request = SandboxRequest(
                    artifact_id=Incident.model_validate(SCENARIOS[scenario].incident()).id,
                    tool=ValidationTool(SCENARIOS[scenario].validator),
                    content=content,
                )
                validation = runner.run(request)
                result["native_recheck"] = validation.model_dump(mode="json")
                native_passed = validation.exit_code == 0 and not validation.timed_out
                result["passed"] = native_passed and result["semantic_match"]
                result["native_validation"] = "passed" if native_passed else "failed"
                result["failure"] = (
                    None
                    if result["passed"]
                    else "scenario or declarative-only safety assertions failed"
                    if not result["semantic_match"]
                    else "native validator failed"
                )
            else:
                result["failure"] = "scenario or declarative-only safety assertions failed"
        records.append(result)
    elapsed = [trial["generation_seconds"] for trial in records]
    median = statistics.median(elapsed)
    p95 = sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1]
    report = original | {
        "trials": records,
        "assessment_note": "Rechecked unmodified saved outputs; empty YAML documents are ignored, "
        "as in kubeconform. Original scores retained. No inference or output repairs.",
        "original_report_sha256": hashlib.sha256(raw).hexdigest(),
        "median_generation_seconds": median,
        "p95_generation_seconds": p95,
        "eligible_for_selectable_mode": meets_gate(records),
    }
    with (output / "assessment.json").open("x") as destination:
        destination.write(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 4))
    parser.add_argument(
        "--recheck", action="store_true", help="Revalidate saved outputs; no inference"
    )
    args = parser.parse_args()
    if args.recheck:
        report = assess_existing(args.tools, args.output)
        print(json.dumps({key: value for key, value in report.items() if key != "trials"}))
        return
    with httpx.Client(base_url="http://127.0.0.1:11434", trust_env=False, timeout=10) as client:
        version = client.get("/api/version")
        version.raise_for_status()
        tags = client.get("/api/tags")
        tags.raise_for_status()
        model_info = next(m for m in tags.json()["models"] if m["name"] == "qwen3:8b")
        show = client.post("/api/show", json={"model": "qwen3:8b"})
        show.raise_for_status()
        if show.json().get("remote_host") or show.json().get("remote_model"):
            raise ValueError("Benchmark requires an installed local model, not cloud inference")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    report = {
        "model": "qwen3:8b",
        "model_digest": model_info["digest"],
        "server_version": version.json()["version"],
        "started_at": datetime.now(UTC).isoformat(),
        "tool_provenance": json.loads((args.tools / "tool-provenance.json").read_text()),
        "scope": "Fixed scenarios include the desired change; not open-ended diagnosis",
        "think": False,
        "temperature": 0,
        "num_predict": 1200,
        "num_ctx": 8192,
        "retries": 0,
        "validation_environment": "native Darwin arm64, not Docker; no runtime recovery tested",
        "gate": {
            "trials_required": 9,
            "all_pass": True,
            "median_generation_seconds_max": 60,
            "p95_generation_seconds_max": 90,
        },
        "trials": results,
        "eligible_for_selectable_mode": False,
    }
    for repeat in range(1, args.repeats + 1):
        for scenario in SCENARIOS:
            print(f"Starting {scenario.value}, trial {repeat}/{args.repeats}", flush=True)
            result = run_trial(scenario, repeat, args.tools, args.output)
            results.append(result)
            elapsed = [r["generation_seconds"] for r in results]
            report["median_generation_seconds"] = statistics.median(elapsed)
            report["p95_generation_seconds"] = sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1]
            report["eligible_for_selectable_mode"] = meets_gate(results)
            (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in [
                            "scenario",
                            "repeat",
                            "passed",
                            "native_validation",
                            "generation_seconds",
                            "total_seconds",
                            "failure",
                        ]
                    }
                ),
                flush=True,
            )
    print(json.dumps({key: value for key, value in report.items() if key != "trials"}), flush=True)


if __name__ == "__main__":
    main()
