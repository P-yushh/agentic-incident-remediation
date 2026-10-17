"""FastAPI application factory and live runtime entry point."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from agentic_incident_remediation.api.contracts import (
    ApprovalSubmission,
    ExecutionControlSubmission,
    HealthResponse,
    IncidentSubmission,
)
from agentic_incident_remediation.api.service import RemediationService
from agentic_incident_remediation.config import get_settings
from agentic_incident_remediation.graph import DurableApprovalWorkflow
from agentic_incident_remediation.llm import create_live_model
from agentic_incident_remediation.logging import configure_logging
from agentic_incident_remediation.models import (
    AgentState,
    ApprovalResponse,
    WorkflowStatus,
)
from agentic_incident_remediation.operations import GitHubPullRequestPublisher
from agentic_incident_remediation.persistence import create_redis_checkpointer
from agentic_incident_remediation.sandbox import DockerSandboxRunner
from agentic_incident_remediation.schema_tools import LocalSchemaToolProvider

LOGGER = logging.getLogger(__name__)
TERMINAL_STATUSES = {
    WorkflowStatus.COMPLETED,
    WorkflowStatus.REJECTED,
    WorkflowStatus.FAILED,
    WorkflowStatus.TERMINATED,
}
_DASHBOARD = Path(__file__).with_name("dashboard.html")


def create_app(
    service: RemediationService, *, close_clients: Callable[[], None] | None = None
) -> FastAPI:
    """Create an API bound to an injected workflow service."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await asyncio.to_thread(service.close, wait=True)
            if close_clients is not None:
                close_clients()

    app = FastAPI(
        title="Agentic Incident Remediation",
        version="0.1.0",
        description="Generate, validate, and review infrastructure remediation proposals.",
        lifespan=lifespan,
    )
    app.state.remediation_service = service

    @app.middleware("http")
    async def request_observability(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid4()))
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            service.metrics.record_request(request.method, status_code)
            LOGGER.info(
                "request completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                },
            )
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD.read_text(encoding="utf-8"))

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(service.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post(
        "/api/incidents",
        response_model=AgentState,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_incident(
        payload: IncidentSubmission, background: bool = Query(default=False)
    ) -> AgentState:
        operation = service.enqueue if background else service.start
        return _translate_errors(lambda: operation(payload.model_dump(mode="json")))

    @app.post("/api/executions/{execution_id}/control", response_model=AgentState)
    def control_execution(execution_id: UUID, payload: ExecutionControlSubmission) -> AgentState:
        return _translate_errors(lambda: service.control(execution_id, payload.action))

    @app.get("/api/executions/{execution_id}", response_model=AgentState)
    def get_execution(execution_id: UUID) -> AgentState:
        return _translate_errors(lambda: service.get(execution_id))

    @app.post("/api/executions/{execution_id}/approval", response_model=AgentState)
    def submit_approval(execution_id: UUID, payload: ApprovalSubmission) -> AgentState:
        def decide() -> AgentState:
            response = ApprovalResponse.model_validate(payload.model_dump())
            return service.decide(execution_id, response)

        return _translate_errors(decide)

    @app.get("/api/executions/{execution_id}/events", include_in_schema=False)
    def execution_events(
        execution_id: UUID,
        request: Request,
        follow: bool = Query(default=True),
    ) -> StreamingResponse:
        _translate_errors(lambda: service.get(execution_id))
        return StreamingResponse(
            _state_events(service, execution_id, request, follow=follow),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def create_live_app() -> FastAPI:
    """Build the configured live model, Redis, Docker, and GitHub runtime."""

    settings = get_settings()
    configure_logging(settings)
    model = create_live_model(settings)
    publisher = GitHubPullRequestPublisher.from_settings(settings)
    workflow = DurableApprovalWorkflow(
        model,
        DockerSandboxRunner(),
        publisher,
        repository=settings.github_repository,
        base_branch=settings.github_base_branch,
        schema_tools=LocalSchemaToolProvider(),
        checkpointer=create_redis_checkpointer(settings.redis_url),
        max_retries=settings.max_reflection_attempts,
    )

    def close_clients() -> None:
        model.close()
        publisher.close()

    return create_app(RemediationService(workflow), close_clients=close_clients)


async def _state_events(
    service: RemediationService,
    execution_id: UUID,
    request: Request,
    *,
    follow: bool,
) -> AsyncIterator[str]:
    last_update = None
    while True:
        state = service.get(execution_id)
        fingerprint = (state.updated_at, state.control_status, state.review_ready)
        if fingerprint != last_update:
            payload = state.model_dump(mode="json")
            yield f"event: state\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            last_update = fingerprint
        if not follow or state.status in TERMINAL_STATUSES or await request.is_disconnected():
            return
        await asyncio.sleep(0.5)


def _translate_errors(operation: Callable[[], AgentState]) -> AgentState:
    try:
        return operation()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, ValidationError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def main() -> None:
    """Run the live API with runtime dependencies loaded at startup."""

    uvicorn.run(create_live_app(), host="127.0.0.1", port=8000)
