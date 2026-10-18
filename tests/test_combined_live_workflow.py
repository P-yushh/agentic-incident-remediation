"""Offline tests for the separately opted-in combined live smoke-test safety harness."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentic_incident_remediation.demo import FixtureSandboxRunner, FixtureStructuredModel
from agentic_incident_remediation.llm import FakeStructuredModel, ModelGatewayError
from agentic_incident_remediation.models import RemediationPlan
from agentic_incident_remediation.sandbox import (
    FakeSandboxRunner,
    SandboxExecution,
    ValidationTool,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "combined_live_workflow.py"
SPEC = importlib.util.spec_from_file_location("combined_live_workflow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_combined_harness_runs_both_cases_and_restores_pending_reviews() -> None:
    model = FixtureStructuredModel()
    runner = FixtureSandboxRunner()
    saver = InMemorySaver()

    report = smoke.run_cases(model, runner, saver)
    recovered = smoke.recover_cases(report, runner, saver)

    assert recovered["passed"] is True
    assert recovered["model_calls"] == 4
    assert len(model.requests) == 4
    assert len(runner.requests) == 2
    assert recovered["recovery_model_calls"] == 0
    assert recovered["phase"] == "recovered"
    for case in recovered["cases"]:
        assert all(case["checks"].values())
        assert all(case["recovery_checks"].values())
        assert case["state"]["status"] == "awaiting_approval"
        assert case["state"]["approval"]["decision"] == "pending"
        assert case["state"]["pull_request"] is None


def test_combined_harness_bounds_corrections_without_additional_model_calls() -> None:
    model = FixtureStructuredModel()
    runner = FakeSandboxRunner(
        [
            SandboxExecution(
                tool=tool, exit_code=1, stderr="Synthetic invalid proposal", duration_ms=1
            )
            for tool in (ValidationTool.KUBECONFORM, ValidationTool.TERRAFORM_VALIDATE)
        ]
    )

    report = smoke.run_cases(model, runner, InMemorySaver())

    assert report["passed"] is False
    assert report["model_calls"] == 4
    assert len(model.requests) == 4
    for case in report["cases"]:
        assert case["state"]["status"] == "failed"
        assert "budget exhausted" in case["state"]["failure_reason"]
        assert case["checks"]["no_publication_attempt"] is True
        assert case["state"]["pull_request"] is None


def test_provider_failure_does_not_claim_review_or_retry() -> None:
    model = FakeStructuredModel([ModelGatewayError("synthetic quota error")] * 2)

    report = smoke.run_cases(model, FixtureSandboxRunner(), InMemorySaver())

    assert report["passed"] is False
    assert report["model_calls"] == 2
    assert len(model.requests) == 2
    assert all(case["state"]["status"] == "failed" for case in report["cases"])


def test_global_budget_rejects_additional_scenarios_before_any_calls(monkeypatch) -> None:
    incidents = smoke.synthetic_incidents()
    model = FixtureStructuredModel()
    monkeypatch.setattr(smoke, "synthetic_incidents", lambda: (*incidents, incidents[0]))
    with pytest.raises(ValueError, match="four-call budget"):
        smoke.run_cases(model, FixtureSandboxRunner(), InMemorySaver())
    assert model.requests == []


def test_budget_rejects_a_third_call_before_reaching_the_delegate() -> None:
    model = FixtureStructuredModel()
    budget = smoke.BudgetedModel(model)
    arguments = {
        "system_prompt": "synthetic",
        "user_prompt": str(uuid4()),
        "response_model": RemediationPlan,
    }
    budget.generate_structured(**arguments)
    budget.generate_structured(**arguments)

    with pytest.raises(ModelGatewayError, match="no request sent"):
        budget.generate_structured(**arguments)
    assert budget.calls == 2
    assert len(model.requests) == 2


@pytest.fixture
def opted_in(monkeypatch) -> None:
    monkeypatch.setenv("AIR_RUN_LIVE_WORKFLOW", "1")
    monkeypatch.setenv("AIR_INTEGRATION_REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)


def test_live_opt_in_and_credential_free_recovery(monkeypatch, opted_in) -> None:
    assert smoke.require_opt_in("run") == "redis://127.0.0.1:6379/0"
    with pytest.raises(ValueError, match="without a Gemini credential"):
        smoke.require_opt_in("recover")
    monkeypatch.delenv("GEMINI_API_KEY")
    assert smoke.require_opt_in("recover") == "redis://127.0.0.1:6379/0"
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        smoke.require_opt_in("run")


@pytest.mark.parametrize("credential", ["GITHUB_TOKEN", "HF_TOKEN"])
def test_live_harness_rejects_unnecessary_credentials(monkeypatch, opted_in, credential) -> None:
    monkeypatch.setenv(credential, "synthetic-key")
    with pytest.raises(ValueError, match="must be empty"):
        smoke.require_opt_in("run")


@pytest.mark.parametrize("url", ["redis://example.com/0", "rediss://localhost/0", ""])
def test_live_harness_rejects_nonlocal_redis(monkeypatch, opted_in, url) -> None:
    monkeypatch.setenv("AIR_INTEGRATION_REDIS_URL", url)
    with pytest.raises(ValueError, match="loopback"):
        smoke.require_opt_in("run")


def test_live_harness_rejects_missing_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("AIR_RUN_LIVE_WORKFLOW", raising=False)
    with pytest.raises(ValueError, match="opt-in"):
        smoke.require_opt_in("run")


def test_report_writes_only_synthetic_output_and_rejects_credentials(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-secret")
    smoke.save_report({"passed": True}, tmp_path)
    assert json.loads((tmp_path / "report.json").read_text()) == {"passed": True}
    capsys.readouterr()

    with pytest.raises(ValueError, match="withheld"):
        smoke.save_report({"content": "synthetic-secret"}, tmp_path)
    assert capsys.readouterr().out == ""
    assert json.loads((tmp_path / "report.json").read_text()) == {"passed": True}


def test_live_workflow_is_manual_main_only_with_one_scoped_secret() -> None:
    text = (SCRIPT.parents[1] / ".github/workflows/combined-live-workflow.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "  push:" not in text
    assert "inputs.confirm_live_calls" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "github.event.repository.private == false" in text
    assert text.count("${{ secrets.") == 1
    assert "contents: read" in text
    assert "persist-credentials: false" in text
    assert "upload-artifact" not in text
