"""Loopback-only Actions dashboard. GitHub credentials never enter browser storage."""

import argparse
import hmac
import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from agentic_incident_remediation.actions.github import ActionsError, GitHubActionsClient
from agentic_incident_remediation.actions.models import ImportRun, RunRecord, StartRun
from agentic_incident_remediation.actions.service import ActionsService
from agentic_incident_remediation.actions.store import RunStore
from agentic_incident_remediation.review_scenarios import SCENARIOS

ASSETS = Path(__file__).parents[1] / "api"


def create_actions_app(service: ActionsService, *, port: int = 8001) -> FastAPI:
    nonce = secrets.token_urlsafe(32)
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.github.close()

    app = FastAPI(title="Actions-backed Incident Remediation", lifespan=lifespan)

    @app.middleware("http")
    async def local_security(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            request.headers.get("host") not in hosts
            or request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
        ):
            return JSONResponse(
                {"detail": "loopback access with an approved Host is required"}, 403
            )
        if request.method not in {"GET", "HEAD"} and (
            request.headers.get("origin") not in origins
            or not hmac.compare_digest(
                request.headers.get("x-air-csrf", "").encode("utf-8"), nonce.encode("ascii")
            )
            or request.headers.get("content-type", "").split(";")[0] != "application/json"
        ):
            return JSONResponse({"detail": "same-origin session confirmation is required"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    def translate(operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except LookupError:
            raise HTTPException(404, "run not found in this dashboard") from None
        except ActionsError as error:
            raise HTTPException(502, str(error)) from None
        except ValueError:
            raise HTTPException(
                409, "operation conflicts with local history or an unresolved dispatch"
            ) from None
        except Exception:
            raise HTTPException(502, "GitHub metadata could not be processed safely") from None

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        # The approved workspace layout has its own Actions controller, not prototype data.
        return (ASSETS / "actions-dashboard.html").read_text(encoding="utf-8")

    @app.get("/actions-dashboard.css", include_in_schema=False)
    def stylesheet() -> Response:
        return Response((ASSETS / "actions-dashboard.css").read_text(), media_type="text/css")

    @app.get("/actions-dashboard.js", include_in_schema=False)
    def script() -> Response:
        return Response((ASSETS / "actions-dashboard.js").read_text(), media_type="text/javascript")

    @app.get("/api/runtime")
    def runtime() -> dict[str, str]:
        return {
            "mode": "actions",
            "model": "Gemini on GitHub (at most 2 calls per consent)",
            "validators": "real Docker on GitHub",
            "publication": "required review in GitHub",
            "persistence": "local SQLite history; remote synthetic artifacts expire",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/actions/session")
    def session() -> dict[str, str]:
        return {"csrf": nonce, "repository": service.github.repository}

    @app.get("/api/actions/readiness")
    def readiness() -> Any:
        return translate(service.github.readiness)

    @app.get("/api/actions/scenarios")
    def scenarios() -> list[dict[str, str]]:
        return [spec.public() for spec in SCENARIOS.values()]

    @app.get("/api/actions/runs", response_model=tuple[RunRecord, ...])
    def runs() -> tuple[RunRecord, ...]:
        return service.store.list()

    @app.post("/api/actions/runs", response_model=RunRecord, status_code=202)
    def start(payload: StartRun) -> Any:
        return translate(lambda: service.start(payload.request_id, payload.scenario))

    @app.post("/api/actions/import", response_model=RunRecord)
    def import_run(payload: ImportRun) -> Any:
        return translate(lambda: service.import_run(payload.run_id))

    @app.get("/api/actions/runs/{identifier}", response_model=RunRecord)
    def refresh(identifier: UUID) -> Any:
        return translate(lambda: service.refresh(identifier))

    @app.post("/api/actions/runs/{identifier}/cancel", response_model=RunRecord)
    def cancel(identifier: UUID) -> Any:
        return translate(lambda: service.cancel(identifier))

    @app.get("/api/actions/runs/{identifier}/proposal")
    def proposal(identifier: UUID) -> Response:
        record: RunRecord = translate(lambda: service.store.get(identifier))
        if record.review is None:
            raise HTTPException(404, "verified proposal is not available")
        return Response(
            record.review.content,
            media_type="text/plain",
            headers={
                "Content-Disposition": 'attachment; filename="proposal.'
                + SCENARIOS[record.review.scenario].extension
                + '"',
            },
        )

    @app.get("/api/actions/runs/{identifier}/receipt")
    def receipt(identifier: UUID) -> Response:
        record: RunRecord = translate(lambda: service.store.get(identifier))
        if record.receipt is None:
            raise HTTPException(404, "verified publication receipt is not available")
        return Response(
            record.receipt.model_dump_json(indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="publication.json"',
            },
        )

    return app


def github_credential() -> str:
    """Reuse a server environment token or existing Git helper, without saving a new key."""

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["git", "-c", "credential.interactive=never", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode == 0 and values.get("password", "").strip():
            return values["password"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise ValueError("Configure a server GITHUB_TOKEN or an existing GitHub Git credential helper")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--repository", default="P-yushh/agentic-incident-remediation")
    parser.add_argument(
        "--history", type=Path, default=Path(".agent-runs/actions-dashboard.sqlite3")
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        github = GitHubActionsClient(token=github_credential(), repository=args.repository)
        service = ActionsService(github, RunStore(args.history, args.repository))
    except ValueError:
        parser.error(
            "Configure a valid repository and server-side GitHub credential; no browser key needed"
        )
    uvicorn.run(create_actions_app(service, port=args.port), host="127.0.0.1", port=args.port)
