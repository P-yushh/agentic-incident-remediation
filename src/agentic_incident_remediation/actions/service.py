"""Durable dispatch idempotency and conservative progress/evidence projection."""

from concurrent.futures import ThreadPoolExecutor
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
from agentic_incident_remediation.actions.local_generation import generate_proposal, local_readiness
from agentic_incident_remediation.actions.models import RunRecord
from agentic_incident_remediation.actions.store import RunStore
from agentic_incident_remediation.llm.diagnostics import FAILURE_MESSAGES, FailureCode
from agentic_incident_remediation.models.base import utc_now
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
        "failure_phase": None,
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
            failure_phase="preparing"
            if name == "prepare"
            else "publishing"
            if name == "publish"
            else None,
            failure_url=f"https://github.com/{repository}/actions/runs/{run_id}/job/{identifier}"
            if type(identifier) is int and identifier > 0
            else None,
        )
        break
    return result


class ActionsService:
    def __init__(
        self, github: GitHubActionsClient, store: RunStore, *, experimental_ollama: bool = False
    ) -> None:
        if github.repository != store.repository:
            raise ValueError("client and local history repository must match")
        self.github = github
        self.store = store
        self._lock = RLock()
        self._checked: dict[UUID, float] = {}
        self.experimental_ollama = experimental_ollama
        self._closing = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="air-local-model")

    def close(self) -> None:
        self._closing = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.github.close()

    def start_local(
        self, identifier: UUID, scenario: DemoScenario, output_tokens: int, timeout_seconds: int
    ) -> RunRecord:
        if not self.experimental_ollama:
            raise ValueError("experimental Ollama mode is not enabled")
        if not 128 <= output_tokens <= 8192 or not 10 <= timeout_seconds <= 1200:
            raise ValueError("invalid local inference budget")
        with self._lock:
            try:
                record = self.store.get(identifier)
            except LookupError:
                record = None
            if record is not None:
                if (
                    record.scenario,
                    record.generation_mode,
                    record.ollama_output_tokens,
                    record.ollama_timeout_seconds,
                ) != (scenario, "ollama_local", output_tokens, timeout_seconds):
                    raise ValueError("request ID already belongs to a different generation intent")
                return record
            readiness = self.github.readiness()
            if not readiness.get("ollama_supported"):
                raise ActionsError(
                    "Publish the reviewed Ollama handoff workflow to main before local testing"
                )
            local_readiness()
            record, inserted = self.store.begin(
                RunRecord(
                    id=identifier,
                    repository=self.github.repository,
                    owned=True,
                    scenario=scenario,
                    stage="local_generating",
                    generation_mode="ollama_local",
                    head_sha=readiness["head_sha"],
                    ollama_output_tokens=output_tokens,
                    ollama_timeout_seconds=timeout_seconds,
                    notice="Experimental local generation. Validation and human review "
                    "remain required in GitHub.",
                )
            )
            if inserted:
                self._executor.submit(self._generate_and_dispatch, record)
            elif (
                record.scenario,
                record.generation_mode,
                record.ollama_output_tokens,
                record.ollama_timeout_seconds,
            ) != (scenario, "ollama_local", output_tokens, timeout_seconds):
                raise ValueError("request ID already belongs to a different generation intent")
            return record

    def _generate_and_dispatch(self, record: RunRecord) -> None:
        try:
            assert record.scenario is not None
            proposal = generate_proposal(
                record.scenario,
                output_tokens=record.ollama_output_tokens or 8192,
                timeout_seconds=record.ollama_timeout_seconds or 600,
                should_stop=lambda: self._closing or self.store.get(record.id).stage == "cancelled",
            )
            with self._lock:
                current = self.store.get(record.id)
                if current.stage == "cancelled" or self._closing:
                    return
                readiness = self.github.readiness()
                if (
                    not readiness.get("ollama_supported")
                    or readiness["head_sha"] != record.head_sha
                ):
                    raise ActionsError(
                        "GitHub main changed during generation; inspect before starting a new run"
                    )
                proposal.wire()  # Size-check before recording any dispatch intent.
                self.store.save(current.model_copy(update={"stage": "dispatching", "notice": None}))
                try:
                    run_id = self.github.dispatch(record.scenario, local=proposal)
                except DispatchUnknown as error:
                    self.store.save(
                        current.model_copy(
                            update={"stage": "dispatch_unknown", "notice": str(error)}
                        )
                    )
                    return
                self.store.save(
                    current.model_copy(
                        update={
                            "stage": "queued",
                            "run_id": run_id,
                            "notice": None,
                            "run_url": f"https://github.com/{record.repository}/actions/runs/{run_id}",
                        }
                    )
                )
        except Exception:
            with self._lock:
                current = self.store.get(record.id)
                if current.stage != "cancelled":
                    self.store.save(
                        current.model_copy(
                            update={
                                "stage": "failed",
                                "failure_origin": "local",
                                "failure_phase": "preparing",
                                "failure_reason": "Experimental local generation or GitHub handoff "
                                "failed. Output may be incomplete, invalid or oversized, or the "
                                "model may have timed out. No hosted fallback or automatic retry.",
                                "completed_phases": ("queued",),
                                "finished_at": utc_now(),
                            }
                        )
                    )

    def start(self, identifier: UUID, scenario: DemoScenario = DemoScenario.OOM) -> RunRecord:
        with self._lock:
            try:
                existing = self.store.get(identifier)
                if (
                    existing.scenario or DemoScenario.OOM
                ) != scenario or existing.generation_mode != "gemini":
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
                if (
                    record.scenario or DemoScenario.OOM
                ) != scenario or record.generation_mode != "gemini":
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
                if (
                    review is not None
                    and record.owned
                    and review.generation_mode != record.generation_mode
                ):
                    review, receipt = None, None
                    raise ValueError("evidence generation mode differs from the dispatch intent")
            except Exception:
                # Untrusted metadata/ZIP/model content must not leak URLs or tokens via exceptions.
                notice = "Evidence unavailable or failed verification. Inspect the GitHub run."
            rejection = None
            if (
                completed
                and conclusion == "failure"
                and prepare.get("conclusion") == "success"
                and publish.get("conclusion") == "failure"
                and publish.get("steps") == []
                and receipt is None
            ):
                try:
                    rejection = self.github.review_rejection(record.run_id)
                except ActionsError:
                    rejection = record.review_rejection
                    notice = (
                        notice + " " if notice else ""
                    ) + "Human-review decision unavailable; failure alone does not prove rejection."
                if rejection is not None:
                    stage = "rejected"
            failures = failure_details(jobs, record.repository, record.run_id)
            if rejection is not None:
                failures = dict.fromkeys(failures)
            diagnostic_code: FailureCode | None = None
            completed_phases = []
            if stage == "failed":
                if prepare.get("status") in {"in_progress", "completed"}:
                    completed_phases.append("queued")
                if prepare.get("conclusion") == "success":
                    completed_phases.append("preparing")
                if receipt is not None:
                    completed_phases.append("awaiting_review")
                if failures["failure_step"] == (
                    "Fresh alert through live inference, Docker validation and pending Redis review"
                ):
                    diagnostic_code = record.failure_code or "unknown"
                    try:
                        diagnostic = self.github.failure_diagnostic(prepare)
                        if diagnostic is not None:
                            diagnostic_code = diagnostic.code
                    except ActionsError:
                        pass
                    failures["failure_reason"] = FAILURE_MESSAGES[diagnostic_code]
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
                    "generation_mode": review.generation_mode
                    if review is not None
                    else record.generation_mode,
                    "notice": notice,
                    "review_rejection": rejection,
                    "failure_code": diagnostic_code,
                    "completed_phases": tuple(completed_phases),
                    **failures,
                }
            )
            self.store.save(record)
            self._checked[identifier] = monotonic()
            return record

    def cancel(self, identifier: UUID) -> RunRecord:
        with self._lock:
            record = self.refresh(identifier)
            if record.owned and record.stage == "local_generating" and record.run_id is None:
                record = record.model_copy(
                    update={
                        "stage": "cancelled",
                        "finished_at": utc_now(),
                        "notice": "Local cancellation recorded. The in-flight model request may "
                        "finish, but its output will not be handed to GitHub.",
                    }
                )
                self.store.save(record)
                return record
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
