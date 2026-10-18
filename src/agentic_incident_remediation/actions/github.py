"""Fixed-repository Actions client: dispatch/cancel only; no approval or merge capability."""

import base64
import hashlib
import io
import json
import re
import zipfile
from typing import Any
from urllib.parse import urlparse

import httpx

from agentic_incident_remediation.actions.models import PublicationReceipt, ReviewEvidence
from agentic_incident_remediation.models import AgentState
from agentic_incident_remediation.review_scenarios import (
    SCENARIOS,
    DemoScenario,
    scenario_for_manifest,
)

WORKFLOW = "reviewed-live-demo.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW}"
# Keep the historic OOM source readable. New dispatch requires the multi-scenario source.
LEGACY_WORKFLOW_SHA256 = "52b354ad73239f51aed54816b2596fcd546721fe24bf2885b8f73d91a3c999c6"
WORKFLOW_SHA256 = "65a9eb0e4b3baf11bb72e03ce45ea8b96a23abdeb7ac27499a73dc08a1e9feb1"
MAX_ARCHIVE = 5_000_000


class ActionsError(RuntimeError):
    """Safe, deliberately non-sensitive error suitable for the local UI."""


class DispatchUnknown(ActionsError):
    """The server must never automatically retry a possibly accepted dispatch."""


class GitHubActionsClient:
    def __init__(self, *, token: str, repository: str, client: httpx.Client | None = None) -> None:
        if not token.strip() or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("a GitHub credential and owner/repository are required")
        self.repository = repository
        self.root = f"/repos/{repository}"
        self._client = client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns_client = client is None
        self._verified_shas: dict[str, str] = {}
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Cache-Control": "no-cache",
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(
                method,
                "https://api.github.com" + path,
                headers=self._headers,
                follow_redirects=False,
                **kwargs,
            )
        except httpx.HTTPError:
            raise ActionsError("GitHub could not be reached; no automatic retry was made") from None

    def get(self, path: str) -> Any:
        response = self.request("GET", path)
        if response.status_code != 200:
            raise ActionsError(f"GitHub lookup returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ActionsError("GitHub returned malformed metadata") from None

    def public_repository(self) -> None:
        data = self.get(self.root)
        if data.get("private") is not False or data.get("full_name") != self.repository:
            raise ActionsError("only the configured public repository is permitted")

    def readiness(self) -> dict[str, Any]:
        self.public_repository()
        workflow = self.get(f"{self.root}/actions/workflows/{WORKFLOW}")
        if workflow.get("state") != "active" or workflow.get("path") != WORKFLOW_PATH:
            raise ActionsError("the reviewed live workflow is not active on this repository")
        sha = self.get(f"{self.root}/git/ref/heads/main")["object"]["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ActionsError("main returned an invalid source revision")
        self.verify_source(sha, for_dispatch=True)
        environment = self.get(f"{self.root}/environments/remediation-demo-review")
        if not any(
            item.get("type") == "required_reviewers" and item.get("reviewers")
            for item in environment.get("protection_rules", [])
        ):
            raise ActionsError("the required human-review gate is not configured")
        permissions = self.get(f"{self.root}/actions/permissions/workflow")
        if not permissions.get("can_approve_pull_request_reviews"):
            raise ActionsError("repository policy currently prevents Actions from creating PRs")
        return {
            "ready": True,
            "repository": self.repository,
            "head_sha": sha,
            "maximum_model_calls": 2,
            "message": "Ready for an explicitly confirmed synthetic run",
        }

    def verify_source(self, sha: str, *, for_dispatch: bool = False) -> None:
        if sha in self._verified_shas and (
            not for_dispatch or self._verified_shas[sha] == WORKFLOW_SHA256
        ):
            return
        source = self.get(f"{self.root}/contents/{WORKFLOW_PATH}?ref={sha}")
        try:
            content = base64.b64decode(source["content"])
        except (KeyError, ValueError):
            raise ActionsError("workflow source could not be verified") from None
        digest = hashlib.sha256(content).hexdigest()
        if digest not in {WORKFLOW_SHA256, LEGACY_WORKFLOW_SHA256}:
            raise ActionsError("workflow has changed; review its budget and safety boundary first")
        if for_dispatch and digest != WORKFLOW_SHA256:
            raise ActionsError(
                "The multi-scenario workflow is not published to main yet. "
                "You can preview scenarios and inspect saved runs without inference."
            )
        self._verified_shas[sha] = digest

    def dispatch(self, scenario: DemoScenario = DemoScenario.OOM) -> int:
        try:
            response = self.request(
                "POST",
                f"{self.root}/actions/workflows/{WORKFLOW}/dispatches",
                json={
                    "ref": "main",
                    "inputs": {"confirm_live_demo": True, "scenario": scenario.value},
                    "return_run_details": True,
                },
            )
        except ActionsError:
            raise DispatchUnknown(
                "dispatch outcome is unknown; inspect GitHub; do not retry"
            ) from None
        if 400 <= response.status_code < 500:
            raise ActionsError(f"GitHub rejected dispatch (HTTP {response.status_code})")
        try:
            identifier = response.json()["workflow_run_id"] if response.status_code == 200 else None
        except (ValueError, KeyError, TypeError):
            identifier = None
        if type(identifier) is not int or identifier < 1:
            raise DispatchUnknown("dispatch may have been accepted without a run ID; do not retry")
        return identifier

    def run(self, identifier: int, *, expected_sha: str = "") -> dict[str, Any]:
        self.public_repository()
        data: dict[str, Any] = self.get(f"{self.root}/actions/runs/{identifier}")
        if (
            data.get("id") != identifier
            or data.get("head_branch") != "main"
            or data.get("event") != "workflow_dispatch"
            or data.get("run_attempt") != 1
            or data.get("path", "").split("@")[0] != WORKFLOW_PATH
            or data.get("repository", {}).get("full_name") != self.repository
            or not re.fullmatch(r"[a-f0-9]{40}", data.get("head_sha", ""))
            or (expected_sha and data.get("head_sha") != expected_sha)
        ):
            raise ActionsError(
                "run does not match the configured main-only, first-attempt workflow"
            )
        self.verify_source(data["head_sha"])
        # The fingerprinted historical workflow had only one possible scenario.
        # This identifies the case, not successful validation or publication.
        data["_legacy_oom_only"] = self._verified_shas[data["head_sha"]] == LEGACY_WORKFLOW_SHA256
        return data

    def jobs(self, identifier: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.get(
            f"{self.root}/actions/runs/{identifier}/jobs?per_page=100"
        )["jobs"]
        return result

    def cancel(self, identifier: int) -> None:
        response = self.request("POST", f"{self.root}/actions/runs/{identifier}/cancel")
        if response.status_code != 202:
            raise ActionsError("cancellation was not confirmed; inspect GitHub before retrying")

    def archive(self, artifact: dict[str, Any]) -> dict[str, bytes]:
        """Verify archive integrity; never execute/extract Redis checkpoints to disk."""

        response = self.request("GET", f"{self.root}/actions/artifacts/{int(artifact['id'])}/zip")
        if response.status_code != 302:
            raise ActionsError("review artifact is unavailable or expired")
        location = response.headers.get("location", "")
        parsed = urlparse(location)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not (
                host.endswith(".blob.core.windows.net")
                or host.endswith(".actions.githubusercontent.com")
            )
        ):
            raise ActionsError("artifact redirect is not an approved HTTPS storage endpoint")
        try:
            # A fresh request intentionally has NO GitHub Authorization header.
            download = self._client.send(
                httpx.Request("GET", location), stream=True, follow_redirects=False
            )
            try:
                if download.status_code != 200:
                    raise ActionsError("artifact download failed")
                chunks = bytearray()
                for chunk in download.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_ARCHIVE:
                        raise ActionsError("review archive exceeds the size limit")
            finally:
                download.close()
        except httpx.HTTPError:
            raise ActionsError("artifact download interrupted; no mutation was retried") from None
        raw = bytes(chunks)
        if artifact.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ActionsError("GitHub artifact digest verification failed")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                names = bundle.namelist()
                allowed = {"manifest.json", "proposal.yaml", "dump.rdb", "publication.json"}
                if len(names) != len(set(names)) or any(name not in allowed for name in names):
                    raise ActionsError("unexpected archive file names")
                limits = {
                    "manifest.json": 2_000_000,
                    "proposal.yaml": 200_000,
                    "dump.rdb": 25_000_000,
                    "publication.json": 20_000,
                }
                if any(item.file_size > limits[item.filename] for item in bundle.infolist()):
                    raise ActionsError("uncompressed artifact exceeds the size limit")
                return {name: bundle.read(name) for name in names if name != "dump.rdb"}
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise ActionsError("review archive could not be read safely") from None

    def evidence(
        self,
        run: dict[str, Any],
        review: ReviewEvidence | None,
        receipt: PublicationReceipt | None,
    ) -> tuple[ReviewEvidence | None, PublicationReceipt | None, str | None]:
        identifier = run["id"]
        artifacts = self.get(f"{self.root}/actions/runs/{identifier}/artifacts?per_page=100")[
            "artifacts"
        ]
        notice = None
        for artifact in artifacts:
            name = artifact.get("name")
            if name not in {f"review-bundle-{identifier}-1", f"publication-receipt-{identifier}"}:
                continue
            if artifact.get("workflow_run", {}).get("id") != identifier:
                raise ActionsError("artifact belongs to a different workflow run")
            if artifact.get("expired"):
                notice = "Remote evidence expired; previously verified local evidence is retained."
                continue
            if name.startswith("review-bundle") and review is None:
                review = review_evidence(self.archive(artifact), run, self.repository)
            elif name.startswith("publication-receipt") and receipt is None:
                receipt = PublicationReceipt.model_validate_json(
                    self.archive(artifact)["publication.json"]
                )
                record = receipt.pull_request
                if (
                    record.repository != self.repository
                    or record.url != f"https://github.com/{self.repository}/pull/{record.number}"
                    or not record.branch_name.startswith("remediation/")
                ):
                    raise ActionsError(
                        "publication receipt points to an unexpected repository or branch"
                    )
        if receipt is not None and (
            review is None
            or (
                receipt.execution_id != review.execution_id
                or receipt.approval_id != review.approval_id
                or receipt.artifact_sha256 != review.artifact_sha256
                or receipt.pull_request.branch_name
                != (
                    f"remediation/{review.scenario.value.replace('_', '-')}-"
                    f"{str(receipt.execution_id)[:8]}"
                )
            )
        ):
            raise ActionsError("publication receipt does not match the validated review")
        return review, receipt, notice


def review_evidence(
    files: dict[str, bytes], run: dict[str, Any], repository: str
) -> ReviewEvidence:
    payload = json.loads(files["manifest.json"])
    provenance = payload.get("provenance", {})
    if (
        payload.get("synthetic") is not True
        or payload.get("model_calls") != 2
        or provenance.get("repository") != repository
        or provenance.get("sha") != run["head_sha"]
        or provenance.get("run_id") != str(run["id"])
        or provenance.get("run_attempt") != "1"
    ):
        raise ActionsError("review manifest provenance or model budget is invalid")
    state = AgentState.model_validate(payload["state"])
    scenario = scenario_for_manifest(payload)
    spec = SCENARIOS[scenario]
    if (
        state.status != "awaiting_approval"
        or not state.review_ready
        or state.approval is None
        or state.approval.decision != "pending"
        or state.pull_request is not None
        or len(state.artifacts) != 1
    ):
        raise ActionsError("review manifest is not the expected pending synthetic incident")
    artifact = state.artifacts[0]
    results = [v for v in state.validation_results if v.artifact_id == artifact.id]
    if (
        artifact.kind != spec.artifact_kind
        or files["proposal.yaml"].decode("utf-8") != artifact.content
        or artifact.checksum != payload["artifact_sha256"]
        or state.approval.artifact_ids != (artifact.id,)
        or not results
        or any(
            v.status != "passed" or v.tool != spec.validator or v.exit_code != 0 for v in results
        )
    ):
        raise ActionsError("proposal checksum, approval binding or validation evidence is invalid")
    return ReviewEvidence(
        scenario=scenario,
        validation_tool=spec.validator,
        execution_id=state.execution_id,
        approval_id=state.approval.id,
        occurred_at=state.incident.occurred_at,
        requested_at=state.approval.requested_at,
        artifact_path=artifact.path,
        content=artifact.content,
        artifact_sha256=artifact.checksum,
    )
