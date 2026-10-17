"""Idempotent GitHub pull-request publication behind a narrow capability boundary."""

from base64 import b64decode, b64encode
from collections.abc import Mapping, Sequence
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.models import PullRequestProposal, PullRequestRecord


class OperationsError(RuntimeError):
    """Raised when an approved repository operation cannot complete safely."""


class PullRequestPublisher(Protocol):
    """The only external write capability granted to the remediation workflow."""

    def publish(self, proposal: PullRequestProposal) -> PullRequestRecord:
        """Create or recover one idempotent remediation pull request."""


class FakePullRequestPublisher:
    """Deterministic publisher used by tests and offline demonstrations."""

    def __init__(self, responses: Sequence[PullRequestRecord | Exception] = ()) -> None:
        self._responses = list(responses)
        self.proposals: list[PullRequestProposal] = []

    def publish(self, proposal: PullRequestProposal) -> PullRequestRecord:
        self.proposals.append(proposal)
        if self._responses:
            response = self._responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return PullRequestRecord(
            repository=proposal.repository,
            number=42,
            url=f"https://github.com/{proposal.repository}/pull/42",
            branch_name=proposal.branch_name,
            commit_sha="a" * 40,
        )


class GitHubPullRequestPublisher:
    """Publish one generated file through GitHub's branch, contents, and PR APIs."""

    def __init__(
        self,
        *,
        token: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        if not token.strip():
            raise ValueError("a GitHub token is required")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = client or httpx.Client(base_url=base_url, timeout=30.0)
        self._client.headers.update(headers)
        self._owns_client = client is None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> "GitHubPullRequestPublisher":
        """Build a live publisher only when an explicit repository token is configured."""

        if settings.github_token is None or not settings.github_token.get_secret_value():
            raise ValueError("GITHUB_TOKEN is required for live repository operations")
        return cls(token=settings.github_token.get_secret_value(), client=client)

    def publish(self, proposal: PullRequestProposal) -> PullRequestRecord:
        """Create a branch, commit the artifact, and open a PR without applying infrastructure."""

        existing = self._existing_pull_request(proposal)
        if existing is not None:
            return existing

        branch_sha = self._ensure_branch(proposal)
        commit_sha = self._ensure_artifact(proposal, branch_sha)
        response = self._request(
            "POST",
            f"/repos/{proposal.repository}/pulls",
            expected={201},
            json={
                "title": proposal.title,
                "body": proposal.body,
                "head": proposal.branch_name,
                "base": proposal.base_branch,
            },
        )
        return _pull_request_record(proposal, response, fallback_sha=commit_sha)

    def close(self) -> None:
        """Close the internally-created HTTP client."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GitHubPullRequestPublisher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _existing_pull_request(self, proposal: PullRequestProposal) -> PullRequestRecord | None:
        owner = proposal.repository.split("/", maxsplit=1)[0]
        response = self._request(
            "GET",
            f"/repos/{proposal.repository}/pulls",
            expected={200},
            params={"state": "open", "head": f"{owner}:{proposal.branch_name}"},
        )
        payload = _json_list(response)
        if not payload:
            return None
        first = payload[0]
        if not isinstance(first, Mapping):
            raise OperationsError("GitHub returned a malformed pull request")
        return _pull_request_record(proposal, first)

    def _ensure_branch(self, proposal: PullRequestProposal) -> str:
        branch_path = quote(proposal.branch_name, safe="")
        response = self._request(
            "GET",
            f"/repos/{proposal.repository}/git/ref/heads/{branch_path}",
            expected={200, 404},
        )
        if response.status_code == 200:
            return _nested_string(_json_object(response), "object", "sha")

        base_path = quote(proposal.base_branch, safe="")
        base = self._request(
            "GET",
            f"/repos/{proposal.repository}/git/ref/heads/{base_path}",
            expected={200},
        )
        base_sha = _nested_string(_json_object(base), "object", "sha")
        self._request(
            "POST",
            f"/repos/{proposal.repository}/git/refs",
            expected={201},
            json={"ref": f"refs/heads/{proposal.branch_name}", "sha": base_sha},
        )
        return base_sha

    def _ensure_artifact(self, proposal: PullRequestProposal, branch_sha: str) -> str:
        artifact_path = quote(proposal.artifact.path, safe="/")
        endpoint = f"/repos/{proposal.repository}/contents/{artifact_path}"
        current = self._request(
            "GET",
            endpoint,
            expected={200, 404},
            params={"ref": proposal.branch_name},
        )
        if current.status_code == 200:
            payload = _json_object(current)
            encoding = payload.get("encoding")
            encoded = payload.get("content")
            if encoding != "base64" or not isinstance(encoded, str):
                raise OperationsError("GitHub returned malformed repository content")
            try:
                existing_content = b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise OperationsError("GitHub returned unreadable repository content") from error
            if existing_content != proposal.artifact.content:
                raise OperationsError(
                    "remediation branch already contains different artifact content"
                )
            return branch_sha

        uploaded = self._request(
            "PUT",
            endpoint,
            expected={201},
            json={
                "message": proposal.commit_message,
                "content": b64encode(proposal.artifact.content.encode("utf-8")).decode("ascii"),
                "branch": proposal.branch_name,
            },
        )
        return _nested_string(_json_object(uploaded), "commit", "sha")

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as error:
            raise OperationsError("GitHub repository operation failed") from error
        if response.status_code not in expected:
            raise OperationsError(
                f"GitHub repository operation returned status {response.status_code}"
            )
        return response


def _pull_request_record(
    proposal: PullRequestProposal,
    payload: httpx.Response | Mapping[str, object],
    *,
    fallback_sha: str | None = None,
) -> PullRequestRecord:
    data = _json_object(payload) if isinstance(payload, httpx.Response) else payload
    number = data.get("number")
    url = data.get("html_url")
    if not isinstance(number, int) or not isinstance(url, str):
        raise OperationsError("GitHub returned a malformed pull request")
    sha = fallback_sha
    head = data.get("head")
    if isinstance(head, Mapping) and isinstance(head.get("sha"), str):
        sha = head["sha"]
    if sha is None:
        raise OperationsError("GitHub pull request response omitted the commit SHA")
    try:
        return PullRequestRecord(
            repository=proposal.repository,
            number=number,
            url=url,
            branch_name=proposal.branch_name,
            commit_sha=sha,
        )
    except ValidationError as error:
        raise OperationsError("GitHub returned an invalid pull request record") from error


def _json_object(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise OperationsError("GitHub returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise OperationsError("GitHub returned an unexpected response")
    return payload


def _json_list(response: httpx.Response) -> list[object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise OperationsError("GitHub returned invalid JSON") from error
    if not isinstance(payload, list):
        raise OperationsError("GitHub returned an unexpected response")
    return payload


def _nested_string(payload: Mapping[str, object], outer: str, inner: str) -> str:
    nested = payload.get(outer)
    if not isinstance(nested, Mapping):
        raise OperationsError("GitHub returned an incomplete repository response")
    value = nested.get(inner)
    if not isinstance(value, str):
        raise OperationsError("GitHub returned an incomplete repository response")
    return value
