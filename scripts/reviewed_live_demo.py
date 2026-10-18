"""Manual, main-only synthetic demo with a required GitHub human review between jobs."""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from redis import Redis

from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import FakeStructuredModel, GeminiStructuredModel
from agentic_incident_remediation.llm.budget import BudgetedModel
from agentic_incident_remediation.operations import GitHubPullRequestPublisher
from agentic_incident_remediation.persistence import create_redis_checkpointer
from agentic_incident_remediation.review_demo import (
    PublicationDisabled,
    prepare_review,
    publish_review,
)
from agentic_incident_remediation.review_scenarios import (
    SCENARIOS,
    DemoScenario,
    scenario_for_manifest,
)
from agentic_incident_remediation.sandbox import DockerSandboxRunner, FakeSandboxRunner
from agentic_incident_remediation.schema_tools import LocalSchemaToolProvider

ENVIRONMENT = "remediation-demo-review"
BUNDLE_FILES = ("manifest.json", "proposal.yaml", "dump.rdb")
REDIS_URL = "redis://127.0.0.1:6379/0"


def context() -> dict[str, str]:
    """Do not allow PR-triggered runs, private runners, alternate code refs or reruns."""

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
        or os.environ.get("AIR_CONFIRM_LIVE_DEMO") != "true"
        or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
        or not re.fullmatch(r"[a-f0-9]{40}", sha)
        or not run_id.isdigit()
        or event.get("repository", {}).get("private") is not False
        or event.get("repository", {}).get("full_name") != repository
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{repository}/.github/workflows/reviewed-live-demo.yml@refs/heads/main"
    ):
        raise ValueError("requires an explicitly confirmed first-attempt public main workflow run")
    scenario = DemoScenario(os.environ.get("AIR_DEMO_SCENARIO", DemoScenario.OOM.value))
    return {
        "repository": repository,
        "sha": sha,
        "run_id": run_id,
        "run_attempt": "1",
        "scenario": scenario.value,
    }


def github_get(path: str) -> Any:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20) as client:
        response = client.get(path)
        if response.status_code != 200:
            raise ValueError("GitHub readiness/review lookup failed; no publication authorized")
        return response.json()


def require_review_environment(provenance: dict[str, str]) -> None:
    environment = github_get(f"/repos/{provenance['repository']}/environments/{ENVIRONMENT}")
    if not any(
        rule.get("type") == "required_reviewers" and rule.get("reviewers")
        for rule in environment.get("protection_rules", [])
    ):
        raise ValueError("configure remediation-demo-review with at least one required reviewer")


def approved_reviewer(provenance: dict[str, str]) -> str:
    """A missing gate or an administrative bypass is not evidence of human approval."""

    require_review_environment(provenance)
    reviews = github_get(
        f"/repos/{provenance['repository']}/actions/runs/{provenance['run_id']}/approvals"
    )
    for review in reviews:
        if any(e.get("name") == ENVIRONMENT for e in review.get("environments", [])):
            user = review.get("user", {})
            if review.get("state") == "approved" and user.get("type") == "User":
                return str(user["login"])
            raise ValueError("the relevant environment review was not approved by a human")
    raise ValueError("no explicit human approval exists for this workflow run")


def safe_write(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    for name in ("GEMINI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN"):
        secret = os.environ.get(name, "")
        if secret and secret in text:
            raise ValueError("credential-bearing report withheld")
    path.write_text(text, encoding="utf-8")


def bundle_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in BUNDLE_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 25_000_000:
            raise ValueError("invalid review bundle file")
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def verify_bundle(
    directory: Path, provenance: dict[str, str], expected_digest: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ValueError("a prepare-job bundle digest is required")
    if bundle_digest(directory) != expected_digest:
        raise ValueError("review bundle digest changed")
    manifest: dict[str, Any] = json.loads((directory / "manifest.json").read_text())
    if manifest.get("provenance") != provenance:
        raise ValueError("bundle belongs to a different run, repository, attempt or source commit")
    artifact = manifest["state"]["artifacts"][0]
    if (directory / "proposal.yaml").read_text() != artifact["content"]:
        raise ValueError("proposal differs from the reviewed state")
    return manifest


def container_command(*arguments: str) -> None:
    container = os.environ.get("AIR_REDIS_CONTAINER", "")
    if not re.fullmatch(r"[a-f0-9]{12,64}", container):
        raise ValueError("an exact disposable Redis service container ID is required")
    command = ["docker", *[arg.replace("{container}", container) for arg in arguments]]
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("disposable Redis snapshot operation failed")


def snapshot_redis(directory: Path) -> None:
    with Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5) as redis:
        if (
            redis.config_get("dir").get("dir") != "/data"
            or redis.config_get("appendonly").get("appendonly") != "no"
            or redis.config_get("dbfilename").get("dbfilename") != "dump.rdb"
        ):
            raise ValueError("Redis must use the disposable /data RDB configuration")
        redis.save()
    container_command("cp", "{container}:/data/dump.rdb", str(directory / "dump.rdb"))


def restore_redis(directory: Path) -> None:
    # Stop the fresh service before copying, so shutdown cannot overwrite the reviewed RDB.
    container_command("stop", "{container}")
    container_command("cp", str(directory / "dump.rdb"), "{container}:/data/dump.rdb")
    container_command("start", "{container}")
    with Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1) as redis:
        for _ in range(40):
            try:
                if redis.ping():
                    return
            except Exception:
                pass
            time.sleep(0.25)
    raise ValueError("restored Redis did not become ready")


def summary(text: str) -> None:
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(text + "\n")


def prepare(directory: Path, provenance: dict[str, str]) -> None:
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("HF_TOKEN"):
        raise ValueError("preparation must not receive repository or alternative-provider tokens")
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("configure the GEMINI_API_KEY repository secret")
    with Redis.from_url(REDIS_URL, socket_timeout=5) as redis:
        if redis.dbsize() != 0:
            raise ValueError("preparation requires a fresh empty disposable Redis database")
    directory.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        model_provider="gemini",
        model_name="gemini-3.8-flash",
        max_reflection_attempts=1,
        model_max_output_tokens=1200,
    )
    with GeminiStructuredModel.from_settings(settings) as model:
        budget = BudgetedModel(model)
        workflow = DurableApprovalWorkflow(
            budget,
            DockerSandboxRunner(),
            PublicationDisabled(),
            repository=provenance["repository"],
            base_branch="main",
            schema_tools=LocalSchemaToolProvider(),
            checkpointer=create_redis_checkpointer(REDIS_URL),
            max_retries=1,
        )
        manifest = prepare_review(
            workflow, budget, provenance, DemoScenario(provenance.get("scenario", DemoScenario.OOM))
        )
    safe_write(directory / "manifest.json", manifest)
    (directory / "proposal.yaml").write_text(
        manifest["state"]["artifacts"][0]["content"], encoding="utf-8"
    )
    snapshot_redis(directory)
    digest = bundle_digest(directory)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"bundle_sha256={digest}\n")
    state = manifest["state"]
    summary(
        "## Pending human review\n\n"
        "Download the review bundle from this run and inspect `proposal.yaml` and "
        "`manifest.json` before approving **remediation-demo-review**. "
        "Approval creates a real PR; rejection/cancellation creates none.\n\n"
        f"Execution: `{state['execution_id']}`\n\n"
        f"Approval: `{state['approval']['id']}`\n\n"
        f"Artifact SHA-256: `{manifest['artifact_sha256']}`\n\n"
        f"Bundle SHA-256: `{digest}`\n\n"
        f"Scenario: `{manifest['scenario']}`. "
        "Model invocations: 2 / 2. Real "
        f"{SCENARIOS[DemoScenario(manifest['scenario'])].validator}: "
        "passed. Nothing deployed."
    )


def publish(directory: Path, provenance: dict[str, str]) -> None:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("HF_TOKEN"):
        raise ValueError("publication must run without model credentials")
    manifest = verify_bundle(directory, provenance, os.environ.get("AIR_BUNDLE_SHA256", ""))
    if scenario_for_manifest(manifest) != DemoScenario(
        provenance.get("scenario", DemoScenario.OOM)
    ):
        raise ValueError("review bundle does not match this run's scenario")
    reviewer = approved_reviewer(provenance)
    restore_redis(directory)
    with GitHubPullRequestPublisher(token=os.environ.get("GITHUB_TOKEN", "")) as publisher:
        workflow = DurableApprovalWorkflow(
            FakeStructuredModel([]),
            FakeSandboxRunner([]),
            publisher,
            repository=provenance["repository"],
            base_branch="main",
            schema_tools=LocalSchemaToolProvider(),
            checkpointer=create_redis_checkpointer(REDIS_URL),
            max_retries=1,
        )
        result = publish_review(workflow, manifest, reviewer=reviewer)
    safe_write(directory / "publication.json", result)
    summary(
        "## Approved PR created\n\n"
        f"[Open remediation PR]({result['pull_request']['url']})\n\n"
        f"Same execution: `{result['execution_id']}`\n\n"
        f"Human reviewer: `{reviewer}`\n\n"
        f"Validated artifact SHA-256: `{result['artifact_sha256']}`\n\n"
        "Publication model invocations: 0. No merge or deployment performed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "prepare", "publish"))
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        provenance = context()
        if args.phase == "preflight":
            require_review_environment(provenance)
            summary("Readiness: public main, first attempt, required human reviewers configured.")
        elif args.phase == "prepare":
            prepare(args.bundle, provenance)
        else:
            publish(args.bundle, provenance)
    except Exception:
        # Exceptions can carry model output, request headers or connection details.
        print("Demo stopped safely. Check setup, quota, review evidence and job step status.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
