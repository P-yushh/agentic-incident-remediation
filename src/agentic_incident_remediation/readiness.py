"""Read-only setup checks; never call a model or initialize checkpoint indices."""

import argparse
import json
import subprocess
import sys
from typing import Any

from redis import Redis

from agentic_incident_remediation.config import ModelProvider, Settings
from agentic_incident_remediation.sandbox.runner import TOOL_PROFILES


def check_readiness(
    mode: str, *, settings: Settings | None = None, probe_services: bool = False
) -> dict[str, Any]:
    """Offline mode does not even load .env; live probes require a separate flag."""

    checks = {"python_3_12_or_newer": sys.version_info >= (3, 12)}
    if mode == "offline":
        return {"mode": mode, "checks": checks, "ready": all(checks.values()), "model_calls": 0}
    if mode != "live":
        raise ValueError("mode must be offline or live")
    configured = settings or Settings()
    checks.update(
        hosted_provider=configured.model_provider
        in {ModelProvider.GEMINI, ModelProvider.HUGGINGFACE},
        model_credential=configured.live_model_configured,
        github_credential=bool(
            configured.github_token and configured.github_token.get_secret_value().strip()
        ),
    )
    if probe_services:
        for label, command in (
            ("docker_daemon", ["docker", "info", "--format", "{{.ServerVersion}}"]),
            *(
                (f"image_{tool.value}", ["docker", "image", "inspect", profile.image])
                for tool, profile in TOOL_PROFILES.items()
            ),
        ):
            try:
                checks[label] = (
                    subprocess.run(command, capture_output=True, timeout=5, check=False).returncode
                    == 0
                )
            except (OSError, subprocess.TimeoutExpired):
                checks[label] = False
        try:
            with Redis.from_url(
                configured.redis_url, socket_connect_timeout=3, socket_timeout=3
            ) as redis:
                checks["redis_ping"] = redis.ping() is True
        except Exception:
            # Redis/URL exceptions can include connection details. Never echo them.
            checks["redis_ping"] = False
    return {
        "mode": mode,
        "checks": checks,
        "ready": all(checks.values()) and probe_services,
        "services_probed": probe_services,
        "model_calls": 0,
        "note": (
            "Credentials are checked for presence only; billing and provider quota are unverified."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--probe-services", action="store_true")
    args = parser.parse_args()
    report = check_readiness(args.mode, probe_services=args.probe_services)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ready"] else 1)
