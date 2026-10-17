"""Application service around the durable workflow."""

import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, RLock
from typing import Any, Literal
from uuid import UUID

from agentic_incident_remediation.api.observability import ApiMetrics
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.models import AgentState, ApprovalResponse, WorkflowStatus
from agentic_incident_remediation.models.base import utc_now

LOGGER = logging.getLogger(__name__)
FINISHED = {
    WorkflowStatus.COMPLETED,
    WorkflowStatus.REJECTED,
    WorkflowStatus.FAILED,
    WorkflowStatus.TERMINATED,
}


@dataclass
class _ExecutionSlot:
    lock: RLock = field(default_factory=RLock)
    active: bool = False
    busy: bool = False
    publishing: bool = False
    pending: Literal["pause", "terminate"] | None = None
    requested_at: datetime | None = None
    future: Future[None] | None = None


class RemediationService:
    """Expose workflow operations while recording business-level telemetry."""

    def __init__(
        self,
        workflow: DurableApprovalWorkflow,
        metrics: ApiMetrics | None = None,
        *,
        step_delay_seconds: float = 0,
    ) -> None:
        self.workflow = workflow
        self.metrics = metrics or ApiMetrics()
        self._step_delay = step_delay_seconds
        self._slots: dict[UUID, _ExecutionSlot] = {}
        self._registry_lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="remediation")
        self._closing = Event()

    def start(self, incident: Mapping[str, Any]) -> AgentState:
        state = self.workflow.start(incident)
        self.metrics.record_start(state.status)
        return self.get(state.execution_id)

    def get(self, execution_id: UUID) -> AgentState:
        slot = self._slot(execution_id)
        with slot.lock:
            state = self.workflow.get_state(execution_id).model_copy(
                update={"review_ready": self.workflow.ready_for_review(execution_id)}
            )
            if slot.requested_at is not None and slot.requested_at > state.updated_at:
                state = state.model_copy(update={"updated_at": slot.requested_at})
            if slot.pending:
                return state.model_copy(
                    update={
                        "control_status": (
                            "pause_requested"
                            if slot.pending == "pause"
                            else "termination_requested"
                        ),
                        "updated_at": state.updated_at,
                    }
                )
            return state

    def history(self, execution_id: UUID) -> tuple[AgentState, ...]:
        return self.workflow.get_history(execution_id)

    def decide(self, execution_id: UUID, response: ApprovalResponse) -> AgentState:
        slot = self._slot(execution_id)
        with slot.lock:
            current = self.workflow.get_state(execution_id)
            if (
                slot.active
                or slot.pending
                or slot.publishing
                or current.control_status != "running"
            ):
                raise ValueError("execution is paused, running, or stopping")
            slot.publishing = True
        try:
            state = self.workflow.resume(execution_id, response)
            self.metrics.record_decision(response.decision, state.status)
            return state
        finally:
            with slot.lock:
                slot.publishing = False

    def enqueue(self, incident: Mapping[str, Any]) -> AgentState:
        """Return a persisted incident immediately, then process it in the background."""

        if self._closing.is_set():
            raise ValueError("service is shutting down")
        state = self.workflow.prepare(incident)
        self.metrics.record_start(state.status)
        slot = self._slot(state.execution_id)
        with slot.lock:
            self._schedule(state.execution_id, slot)
        return state

    def control(
        self, execution_id: UUID, action: Literal["pause", "resume", "terminate"]
    ) -> AgentState:
        """Serialize control intent and apply it only between graph nodes."""

        slot = self._slot(execution_id)
        with slot.lock:
            current = self.workflow.get_state(execution_id)
            if current.status in FINISHED:
                raise ValueError("execution has already finished")
            if slot.publishing:
                raise ValueError("repository publication is already in progress")
            if slot.pending == "terminate":
                if action == "terminate":
                    return self.get(execution_id)
                raise ValueError("termination has already been requested")
            if action == "resume":
                if current.control_status != "paused" and slot.pending != "pause":
                    raise ValueError("execution is not paused")
                slot.pending = None
                slot.requested_at = utc_now()
                if not slot.busy:
                    current = self.workflow.set_control(execution_id, "resume")
                self._schedule(execution_id, slot)
                return self.get(execution_id)
            if action == "pause" and self.workflow.ready_for_review(execution_id):
                raise ValueError("execution is already waiting for human review")
            if action == "pause" and current.control_status == "paused":
                return current
            if slot.busy:
                slot.pending = action
                slot.requested_at = utc_now()
                return self.get(execution_id)
            slot.pending = None
            result = self.workflow.set_control(execution_id, action)
            if action == "terminate":
                self.metrics.record_outcome(result.status)
            return self.get(execution_id)

    def _slot(self, execution_id: UUID) -> _ExecutionSlot:
        with self._registry_lock:
            if execution_id not in self._slots:
                self.workflow.get_state(execution_id)
                self._slots[execution_id] = _ExecutionSlot()
            return self._slots[execution_id]

    def _schedule(self, execution_id: UUID, slot: _ExecutionSlot) -> None:
        if self._closing.is_set():
            raise ValueError("service is shutting down")
        if not slot.active:
            slot.active = True
            slot.future = self._executor.submit(self._run, execution_id, slot)

    def _run(self, execution_id: UUID, slot: _ExecutionSlot) -> None:
        try:
            while True:
                self._closing.wait(self._step_delay)
                with slot.lock:
                    current = self.workflow.get_state(execution_id)
                    if current.status in FINISHED:
                        return
                    if slot.pending:
                        action = slot.pending
                        slot.pending = None
                        stopped = self.workflow.set_control(execution_id, action)
                        if action == "terminate":
                            self.metrics.record_outcome(stopped.status)
                        return
                    if current.control_status == "paused":
                        return
                    if self.workflow.ready_for_review(execution_id):
                        return
                    if self._closing.is_set():
                        self.workflow.set_control(execution_id, "pause")
                        return
                    slot.busy = True
                try:
                    state = self.workflow.advance(execution_id)
                finally:
                    with slot.lock:
                        slot.busy = False
                if state.status in FINISHED:
                    self.metrics.record_outcome(state.status)
                    return
        except Exception:
            LOGGER.exception(
                "background execution failed", extra={"execution_id": str(execution_id)}
            )
            with slot.lock:
                current = self.workflow.get_state(execution_id)
                if current.status not in FINISHED:
                    failed = self.workflow.set_control(execution_id, "fail")
                    self.metrics.record_outcome(failed.status)
        finally:
            with slot.lock:
                slot.active = False
                current = self.workflow.get_state(execution_id)
                if current.status in FINISHED:
                    slot.pending = None
                elif (
                    current.control_status == "running"
                    and not self.workflow.ready_for_review(execution_id)
                    and not self._closing.is_set()
                ):
                    self._schedule(execution_id, slot)

    def close(self, *, wait: bool = False) -> None:
        """Drain active steps without starting further model or repository operations."""

        self._closing.set()
        with self._registry_lock:
            slots = tuple(self._slots.items())
        for execution_id, slot in slots:
            with slot.lock:
                current = self.workflow.get_state(execution_id)
                if not slot.active or current.status in FINISHED:
                    continue
                if slot.busy:
                    if slot.pending != "terminate":
                        slot.pending = "pause"
                        slot.requested_at = utc_now()
                elif not self.workflow.ready_for_review(execution_id):
                    self.workflow.set_control(execution_id, "pause")
        self._executor.shutdown(wait=wait, cancel_futures=True)
