"""Cooperative controls must stop work, not merely alter dashboard labels."""

from threading import Event
from typing import TypeVar
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from agentic_incident_remediation.api import RemediationService, create_app
from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.models import ApprovalResponse, WorkflowStatus
from agentic_incident_remediation.operations import FakePullRequestPublisher

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
REPOSITORY = "P-yushh/agentic-incident-remediation"


class BlockingModel(FixtureStructuredModel):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.fail = fail

    def generate_structured(
        self, *, system_prompt: str, user_prompt: str, response_model: type[ResponseModel]
    ) -> ResponseModel:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release the model")
        if self.fail:
            raise RuntimeError("unexpected model error")
        return super().generate_structured(
            system_prompt=system_prompt, user_prompt=user_prompt, response_model=response_model
        )


def alert() -> dict[str, str]:
    return {
        "type": "kubernetes_oom_killed",
        "title": "Payment API memory limit exceeded",
        "description": "Synthetic alert for execution control tests.",
    }


def workflow(
    saver: InMemorySaver,
    model: FixtureStructuredModel,
    publisher: FakePullRequestPublisher,
) -> DurableApprovalWorkflow:
    return DurableApprovalWorkflow(
        model,
        FixtureSandboxRunner(),
        publisher,
        repository=REPOSITORY,
        checkpointer=saver,
    )


def wait_for_idle(service: RemediationService, execution_id: UUID) -> None:
    future = service._slots[execution_id].future
    assert future is not None
    future.result(timeout=5)


def test_pause_waits_for_current_step_and_resume_does_not_restart_planning() -> None:
    model = BlockingModel()
    publisher = FakePullRequestPublisher()
    service = RemediationService(workflow(InMemorySaver(), model, publisher))
    try:
        created = service.enqueue(alert())
        assert created.status is WorkflowStatus.RECEIVED
        assert model.entered.wait(timeout=5)

        requested = service.control(created.execution_id, "pause")
        assert requested.control_status == "pause_requested"
        assert service.get(created.execution_id).control_status == "pause_requested"
        model.release.set()
        wait_for_idle(service, created.execution_id)

        paused = service.get(created.execution_id)
        assert paused.control_status == "paused"
        assert paused.status is WorkflowStatus.GENERATING
        assert len(model.requests) == 1
        with pytest.raises(ValueError, match="paused"):
            service.workflow.advance(created.execution_id)
        assert service.control(created.execution_id, "pause").control_status == "paused"

        resumed = service.control(created.execution_id, "resume")
        assert resumed.execution_id == created.execution_id
        wait_for_idle(service, created.execution_id)
        review = service.get(created.execution_id)
        assert review.review_ready
        assert review.status is WorkflowStatus.AWAITING_APPROVAL
        assert len(model.requests) == 2
        assert publisher.proposals == []
    finally:
        model.release.set()
        service.close()


def test_termination_during_a_call_is_persisted_and_prevents_further_work() -> None:
    saver = InMemorySaver()
    model = BlockingModel()
    publisher = FakePullRequestPublisher()
    service = RemediationService(workflow(saver, model, publisher))
    try:
        created = service.enqueue(alert())
        assert model.entered.wait(timeout=5)
        stopping = service.control(created.execution_id, "terminate")
        assert stopping.control_status == "termination_requested"
        assert (
            service.control(created.execution_id, "terminate").control_status
            == "termination_requested"
        )
        with pytest.raises(ValueError, match="termination"):
            service.control(created.execution_id, "resume")
        model.release.set()
        wait_for_idle(service, created.execution_id)

        terminated = service.get(created.execution_id)
        assert terminated.status is WorkflowStatus.TERMINATED
        assert terminated.artifacts == ()
        assert len(model.requests) == 1
        restarted = workflow(saver, FixtureStructuredModel(), publisher)
        assert restarted.get_state(created.execution_id).status is WorkflowStatus.TERMINATED
        with pytest.raises(ValueError, match="not awaiting"):
            restarted.resume(
                created.execution_id,
                ApprovalResponse(
                    approval_id=uuid4(), decision="approved", decided_by="test-reviewer"
                ),
            )
        for action in ("pause", "resume", "terminate"):
            with pytest.raises(ValueError, match="finished"):
                service.control(created.execution_id, action)
        assert publisher.proposals == []
        assert 'remediation_workflow_outcomes_total{status="terminated"} 1.0' in (
            service.metrics.render().decode()
        )
    finally:
        model.release.set()
        service.close()


def test_paused_execution_can_resume_with_a_recreated_service() -> None:
    saver = InMemorySaver()
    publisher = FakePullRequestPublisher()
    original = workflow(saver, FixtureStructuredModel(), publisher)
    created = original.prepare(alert())
    original.set_control(created.execution_id, "pause")
    service = RemediationService(workflow(saver, FixtureStructuredModel(), publisher))
    try:
        assert service.get(created.execution_id).control_status == "paused"
        service.control(created.execution_id, "resume")
        wait_for_idle(service, created.execution_id)
        assert service.get(created.execution_id).review_ready
    finally:
        service.close()


def test_resume_can_cancel_a_pending_pause_without_a_timer_timestamp_rollback() -> None:
    model = BlockingModel()
    service = RemediationService(workflow(InMemorySaver(), model, FakePullRequestPublisher()))
    try:
        created = service.enqueue(alert())
        assert model.entered.wait(timeout=5)
        pause = service.control(created.execution_id, "pause")
        resume = service.control(created.execution_id, "resume")
        assert resume.control_status == "running"
        assert resume.updated_at >= pause.updated_at
        model.release.set()
        wait_for_idle(service, created.execution_id)
        assert service.get(created.execution_id).review_ready
    finally:
        model.release.set()
        service.close()


def test_controls_are_isolated_between_incidents() -> None:
    model = BlockingModel()
    publisher = FakePullRequestPublisher()
    service = RemediationService(workflow(InMemorySaver(), model, publisher))
    try:
        first = service.enqueue(alert())
        assert model.entered.wait(timeout=5)
        second = service.enqueue(alert())
        service.control(first.execution_id, "terminate")
        model.release.set()
        wait_for_idle(service, first.execution_id)
        wait_for_idle(service, second.execution_id)
        assert service.get(first.execution_id).status is WorkflowStatus.TERMINATED
        assert service.get(second.execution_id).review_ready
        assert publisher.proposals == []
    finally:
        model.release.set()
        service.close()


def test_idle_controls_and_application_shutdown() -> None:
    service = RemediationService(
        workflow(InMemorySaver(), FixtureStructuredModel(), FakePullRequestPublisher())
    )
    created = service.workflow.prepare(alert())
    paused = service.control(created.execution_id, "pause")
    assert paused.control_status == "paused"
    assert service.control(created.execution_id, "terminate").status is WorkflowStatus.TERMINATED
    closed = []
    with TestClient(create_app(service, close_clients=lambda: closed.append(True))) as client:
        assert client.get("/health").status_code == 200
    assert closed == [True]


def test_review_cannot_be_paused_or_resumed_but_can_be_terminated() -> None:
    publisher = FakePullRequestPublisher()
    service = RemediationService(workflow(InMemorySaver(), FixtureStructuredModel(), publisher))
    try:
        review = service.start(alert())
        assert review.approval is not None
        for action in ("pause", "resume"):
            with pytest.raises(ValueError):
                service.control(review.execution_id, action)
        stopped = service.control(review.execution_id, "terminate")
        assert stopped.status is WorkflowStatus.TERMINATED
        with pytest.raises(ValueError):
            service.decide(
                review.execution_id,
                ApprovalResponse(
                    approval_id=review.approval.id, decision="approved", decided_by="test-reviewer"
                ),
            )
        assert publisher.proposals == []
    finally:
        service.close()


def test_background_api_exposes_requested_controls_and_terminal_events() -> None:
    model = BlockingModel()
    service = RemediationService(workflow(InMemorySaver(), model, FakePullRequestPublisher()))
    try:
        with TestClient(create_app(service)) as client:
            created = client.post("/api/incidents?background=true", json=alert())
            assert created.status_code == 202
            execution_id = UUID(created.json()["execution_id"])
            assert model.entered.wait(timeout=5)
            base = f"/api/executions/{execution_id}"
            assert (
                client.post(base + "/control", json={"action": "pause"}).json()["control_status"]
                == "pause_requested"
            )
            events = client.get(base + "/events?follow=false")
            assert '"control_status":"pause_requested"' in events.text
            assert client.post(base + "/control", json={"action": "delete"}).status_code == 422
            assert (
                client.post(
                    f"/api/executions/{uuid4()}/control", json={"action": "pause"}
                ).status_code
                == 404
            )
            client.post(base + "/control", json={"action": "terminate"})
            model.release.set()
            wait_for_idle(service, execution_id)
            assert client.get(base).json()["status"] == "terminated"
            assert '"status":"terminated"' in client.get(base + "/events").text
            assert client.post(base + "/control", json={"action": "resume"}).status_code == 409
    finally:
        model.release.set()
        service.close()


def test_background_exception_fails_safely_and_shutdown_pauses_running_work() -> None:
    for fail in (True, False):
        model = BlockingModel(fail=fail)
        publisher = FakePullRequestPublisher()
        service = RemediationService(workflow(InMemorySaver(), model, publisher))
        try:
            created = service.enqueue(alert())
            assert model.entered.wait(timeout=5)
            if not fail:
                service.close()
            model.release.set()
            wait_for_idle(service, created.execution_id)
            state = service.get(created.execution_id)
            if fail:
                assert state.status is WorkflowStatus.FAILED
                assert state.failure_reason == "background execution could not complete safely"
            else:
                assert state.control_status == "paused"
                with pytest.raises(ValueError, match="shutting down"):
                    service.enqueue(alert())
            assert publisher.proposals == []
        finally:
            model.release.set()
            service.close()
