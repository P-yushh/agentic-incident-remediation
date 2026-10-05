"""Locked-down Docker runner and deterministic test double."""

import subprocess
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic_ns
from typing import Protocol

from agentic_incident_remediation.sandbox.models import (
    SandboxExecution,
    SandboxRequest,
    ValidationTool,
)


class SandboxError(RuntimeError):
    """Base error for sandbox configuration or availability failures."""


class SandboxUnavailableError(SandboxError):
    """Raised when the configured container runtime cannot be started."""


class SandboxRunner(Protocol):
    """Execution boundary used by the validator node."""

    def run(self, request: SandboxRequest) -> SandboxExecution:
        """Execute an allowlisted validator in isolation."""


class ProcessExecutor(Protocol):
    """Narrow subprocess surface injected for deterministic command tests."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DockerToolProfile:
    """Pinned image and fixed arguments for one validator."""

    image: str
    arguments: tuple[str, ...] = ()


TOOL_PROFILES = {
    ValidationTool.KUBECONFORM: DockerToolProfile(
        image="agentic-remediation/kubeconform-validator:0.1.0",
    ),
    ValidationTool.TERRAFORM_VALIDATE: DockerToolProfile(
        image="agentic-remediation/terraform-validator:0.1.0",
    ),
}


class DockerSandboxRunner:
    """Run fixed validator images without shell interpolation or host mounts."""

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        timeout_seconds: float = 20.0,
        memory_limit: str = "256m",
        cpu_limit: str = "0.50",
        pids_limit: int = 64,
        executor: ProcessExecutor = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("sandbox timeout must be positive")
        if pids_limit < 1:
            raise ValueError("sandbox PID limit must be positive")
        self._docker_binary = docker_binary
        self._timeout_seconds = timeout_seconds
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._pids_limit = pids_limit
        self._executor = executor

    def run(self, request: SandboxRequest) -> SandboxExecution:
        profile = TOOL_PROFILES[request.tool]
        command = self._build_command(profile)
        started = monotonic_ns()
        try:
            completed = self._executor(
                command,
                input=request.content,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise SandboxUnavailableError(
                f"container runtime is unavailable: {self._docker_binary}"
            ) from error
        except subprocess.TimeoutExpired as error:
            return SandboxExecution(
                tool=request.tool,
                exit_code=-1,
                stdout=_bounded_text(error.stdout),
                stderr=_bounded_text(error.stderr) or "sandbox validation timed out",
                duration_ms=_elapsed_ms(started),
                timed_out=True,
            )

        return SandboxExecution(
            tool=request.tool,
            exit_code=completed.returncode,
            stdout=_bounded_text(completed.stdout),
            stderr=_bounded_text(completed.stderr),
            duration_ms=_elapsed_ms(started),
        )

    def _build_command(self, profile: DockerToolProfile) -> tuple[str, ...]:
        return (
            self._docker_binary,
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._pids_limit),
            "--memory",
            self._memory_limit,
            "--cpus",
            self._cpu_limit,
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            profile.image,
            *profile.arguments,
        )


class FakeSandboxRunner:
    """Queue sandbox results or errors without Docker for tests and offline demos."""

    def __init__(self, responses: Sequence[SandboxExecution | SandboxError]) -> None:
        self._responses = deque(responses)
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxExecution:
        self.requests.append(request)
        if not self._responses:
            raise SandboxUnavailableError("fake sandbox has no queued response")
        response = self._responses.popleft()
        if isinstance(response, SandboxError):
            raise response
        if response.tool is not request.tool:
            raise SandboxError(
                f"fake sandbox returned {response.tool.value} for {request.tool.value}"
            )
        return response


def _bounded_text(value: str | bytes | None, *, limit: int = 20_000) -> str:
    if value is None:
        return ""
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    return text[-limit:]


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)
