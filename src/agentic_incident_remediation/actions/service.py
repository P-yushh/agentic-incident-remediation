"""Durable dispatch idempotency and conservative progress/evidence projection."""

from datetime import datetime
from threading import RLock
from time import monotonic
from typing import Any
from uuid import UUID

from agentic_incident_remediation.actions.github import (
    ActionsError,
    DispatchUnknown,
    GitHubActionsClient,
)
from agentic_incident_remediation.actions.models import RunRecord
from agentic_incident_remediation.actions.store import RunStore
from agentic_incident_remediation.review_scenarios import DemoScenario

SAFE_STEPS = {
    "Set up job",
    "Initialize containers",
    "Install project",
    "Verify required reviewers before spending model quota",
    "Build real Kubernetes validator without model credentials",
    "Build real Terraform validator without model credentials",
    "Fresh alert through live inference, Docker validation and pending Redis review",
    "Save synthetic proposal, review evidence and original Redis checkpoints",
    "Install project without provider credentials",
    "Download only this run's reviewed checkpoints",
    "Verify human review, restore exact execution and publish approved artifact",
    "Save synthetic publication receipt",
    "Stop containers",
}


def failure_details(
    jobs: list[dict[str, Any]], repository: str, run_id: int
) -> dict[str, str | None]:
    """Expose bounded known step labels, never raw logs, exceptions or annotation text."""
    result: dict[str, str | None] = {
        "failure_job": None,
        "failure_step": None,
        "failure_reason": None,
        "failure_url": None,
    }
    for job in jobs:
        if job.get("conclusion") not in {
            "failure",
            "timed_out",
            "startup_failure",
            "action_required",
        }:
            continue
        name = job.get("name") if job.get("name") in {"prepare", "publish"} else "workflow"
        step: dict[str, Any] = next(
            (s for s in job.get("steps", []) if s.get("conclusion") in {"failure", "timed_out"}), {}
        )
        label = step.get("name")
        safe_label = label if label in SAFE_STEPS else "Inspect the failed job in GitHub"
        reason = "The remote job failed. Inspect its logs for the underlying error."
        if job.get("conclusion") == "timed_out":
            reason = "The remote job exceeded its time limit. Inspect GitHub before retrying."
        elif label in SAFE_STEPS and str(label).startswith("Build real"):
            reason = (
                "Validator image build failed before model inference. "
                "Inspect dependency availability and the build output."
            )
        elif label == "Verify required reviewers before spending model quota":
            reason = (
                "Required-review preflight failed before inference. "
                "Check the repository review environment."
            )
        elif (
            label
            == "Fresh alert through live inference, Docker validation and pending Redis review"
        ):
            reason = (
                "Generation or validation failed. Check provider quota, validator findings "
                "and checkpoint setup in GitHub; no new proposal is marked verified here."
            )
        elif name == "publish":
            reason = (
                "PR publication did not complete cleanly. Check the run and repository "
                "before retrying; an existing PR is not undone."
            )
        identifier = job.get("id")
        result.update(
            failure_job=str(name),
            failure_step=str(safe_label),
            failure_reason=reason,
            failure_url=f"https://github.com/{repository}/actions/runs/{run_id}/job/{identifier}"
            if type(identifier) is int and identifier > 0
            else None,
        )
        break
    return result


class ActionsService:
    def __init__(self, github: GitHubActionsClient, store: RunStore) -> None:
        if github.repository != store.repository:
            raise ValueError("client and local history repository must match")
        self.github = github
        self.store = store
        self._lock = RLock()
        self._checked: dict[UUID, float] = {}

    def start(self, identifier: UUID, scenario: DemoScenario = DemoScenario.OOM) -> RunRecord:
        with self._lock:
            try:
                existing = self.store.get(identifier)
                if (existing.scenario or DemoScenario.OOM) != scenario:
                    raise ValueError("request ID already belongs to a different scenario")
                return existing
            except LookupError:
                pass
            readiness = self.github.readiness()
            record, inserted = self.store.begin(
                RunRecord(
                    id=identifier,
                    repository=self.github.repository,
                    owned=True,
                    scenario=scenario,
                    stage="dispatching",
                    head_sha=readiness["head_sha"],
                )
            )
            if not inserted:
                if (record.scenario or DemoScenario.OOM) != scenario:
                    raise ValueError("request ID already belongs to a different scenario")
                return record
            try:
                run_id = self.github.dispatch(scenario)
            except DispatchUnknown as error:
                record = record.model_copy(
                    update={"stage": "dispatch_unknown", "notice": str(error)}
                )
            except ActionsError as error:
                record = record.model_copy(update={"stage": "failed", "notice": str(error)})
            else:
                record = record.model_copy(
                    update={
                        "run_id": run_id,
                        "stage": "queued",
                        "run_url": f"https://github.com/{record.repository}/actions/runs/{run_id}",
                    }
                )
            self.store.save(record)
            return record

    def import_run(self, run_id: int) -> RunRecord:
        """Import only for inspection; importing cannot start, cancel or approve a run."""

        with self._lock:
            for item in self.store.list():
                if item.run_id == run_id:
                    return self.refresh(item.id)
            run = self.github.run(run_id)
            record = RunRecord(
                repository=self.github.repository,
                head_sha=run["head_sha"],
                run_id=run_id,
                run_url=f"https://github.com/{self.github.repository}/actions/runs/{run_id}",
            )
            self.store.save(record)
            return self.refresh(record.id)

    def refresh(self, identifier: UUID) -> RunRecord:
        with self._lock:
            record = self.store.get(identifier)
            if record.run_id is None or monotonic() - self._checked.get(identifier, -100) < 5:
                return record
            run = self.github.run(record.run_id, expected_sha=record.head_sha)
            inferred_scenario = record.scenario or (
                DemoScenario.OOM if run.get("_legacy_oom_only") is True else None
            )
            jobs = self.github.jobs(record.run_id)
            completed = run.get("status") == "completed"
            conclusion = run.get("conclusion")
            prepare = next((job for job in jobs if job["name"] == "prepare"), {})
            publish = next((job for job in jobs if job["name"] == "publish"), {})
            if completed:
                stage = (
                    "completed"
                    if conclusion == "success"
                    else ("cancelled" if conclusion == "cancelled" else "failed")
                )
            elif publish.get("status") == "in_progress":
                stage = "publishing"
            elif prepare.get("conclusion") == "success" and run.get("status") == "waiting":
                stage = "awaiting_review"
            else:
                stage = "preparing" if prepare.get("status") == "in_progress" else "queued"
            steps = tuple(
                f"{job['name']}: {step['name']}"
                for job in jobs
                for step in job.get("steps", [])
                if step.get("status") == "in_progress"
            )
            review, receipt, notice = record.review, record.receipt, None
            try:
                review, receipt, notice = self.github.evidence(run, review, receipt)
                if (
                    review is not None
                    and record.scenario is not None
                    and review.scenario != record.scenario
                ):
                    review, receipt = None, None
                    raise ValueError("evidence scenario differs from the dispatch intent")
            except Exception:
                # Untrusted metadata/ZIP/model content must not leak URLs or tokens via exceptions.
                notice = "Evidence unavailable or failed verification. Inspect the GitHub run."
            record = record.model_copy(
                update={
                    "stage": stage,
                    "conclusion": conclusion,
                    "steps": steps,
                    "started_at": datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")),
                    "finished_at": datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                    if completed
                    else None,
                    "review": review,
                    "receipt": receipt,
                    "scenario": review.scenario if review is not None else inferred_scenario,
                    "notice": notice,
                    **failure_details(jobs, record.repository, record.run_id),
                }
            )
            self.store.save(record)
            self._checked[identifier] = monotonic()
            return record

    def cancel(self, identifier: UUID) -> RunRecord:
        with self._lock:
            record = self.refresh(identifier)
            if not record.owned or not record.active or record.run_id is None:
                raise ValueError("only an active run started by this dashboard may be cancelled")
            self.github.cancel(record.run_id)
            record = record.model_copy(
                update={
                    "notice": (
                        "Cancellation requested; existing PRs and in-flight writes are not undone."
                    )
                }
            )
            self.store.save(record)
            self._checked.pop(identifier, None)
            return record
