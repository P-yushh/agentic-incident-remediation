"""No-cost readiness checks and explicit dashboard runtime labels."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agentic_incident_remediation import readiness
from agentic_incident_remediation.config import Settings
from agentic_incident_remediation.demo import create_demo_app


def settings(**kwargs):
    return Settings(
        _env_file=None,
        model_provider="gemini",
        gemini_api_key="synthetic",
        github_token="synthetic",
        **kwargs,
    )


def test_offline_readiness_does_not_load_settings_or_probe_services(monkeypatch) -> None:
    monkeypatch.setattr(readiness, "Settings", Mock(side_effect=AssertionError(".env read")))
    monkeypatch.setattr(readiness.subprocess, "run", Mock(side_effect=AssertionError("process")))
    monkeypatch.setattr(readiness.Redis, "from_url", Mock(side_effect=AssertionError("Redis")))
    assert readiness.check_readiness("offline", probe_services=True)["ready"] is True


def test_live_presence_check_does_not_claim_runtime_or_quota_verification() -> None:
    report = readiness.check_readiness("live", settings=settings())
    assert all(report["checks"].values())
    assert report["ready"] is False
    assert report["model_calls"] == 0
    assert "synthetic" not in json.dumps(report)


def test_live_service_probes_are_read_only(monkeypatch) -> None:
    process = Mock(return_value=SimpleNamespace(returncode=0))
    redis = Mock()
    redis.__enter__ = Mock(return_value=redis)
    redis.__exit__ = Mock(return_value=None)
    redis.ping.return_value = True
    monkeypatch.setattr(readiness.subprocess, "run", process)
    monkeypatch.setattr(readiness.Redis, "from_url", Mock(return_value=redis))
    report = readiness.check_readiness("live", settings=settings(), probe_services=True)
    assert report["ready"] is True
    assert process.call_count == 3
    assert all(
        call.args[0][:2] in (["docker", "info"], ["docker", "image"])
        for call in process.call_args_list
    )
    redis.ping.assert_called_once()


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("docker", 5)])
def test_unavailable_services_are_reported_without_connection_details(monkeypatch, error) -> None:
    monkeypatch.setattr(readiness.subprocess, "run", Mock(side_effect=error))
    monkeypatch.setattr(readiness.Redis, "from_url", Mock(side_effect=ValueError("secret URL")))
    report = readiness.check_readiness("live", settings=settings(), probe_services=True)
    assert report["ready"] is False
    assert report["checks"]["redis_ping"] is False
    assert "secret URL" not in json.dumps(report)


def test_missing_credentials_and_nonlive_provider_do_not_pass() -> None:
    configured = Settings(_env_file=None, model_provider="fixture", github_token=" ")
    report = readiness.check_readiness("live", settings=configured)
    assert report["checks"]["hosted_provider"] is False
    assert report["checks"]["github_credential"] is False
    with pytest.raises(ValueError, match="mode"):
        readiness.check_readiness("unsupported")


def test_cli_prints_safe_report_and_exits(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["check", "--mode", "offline"])
    with pytest.raises(SystemExit) as exit_info:
        readiness.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0


def test_offline_dashboard_labels_simulated_capabilities() -> None:
    with TestClient(create_demo_app()) as client:
        runtime = client.get("/api/runtime").json()
        assert runtime["mode"] == "offline"
        assert runtime["validators"] == "simulated"
        assert "simulated PR" in runtime["publication"]
        assert "runtime-mode" in client.get("/").text
