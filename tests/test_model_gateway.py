"""Structured model gateway tests that never call a hosted model."""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agentic_incident_remediation.config import ModelProvider, Settings
from agentic_incident_remediation.llm import (
    FakeStructuredModel,
    HuggingFaceStructuredModel,
    ModelGatewayError,
    ModelOutputError,
)
from agentic_incident_remediation.models import (
    PlanStep,
    PlanStepKind,
    RemediationPlan,
)


def make_plan_payload() -> dict[str, Any]:
    return {
        "incident_id": str(uuid4()),
        "summary": "Increase the memory limit after validating observed usage.",
        "steps": [
            {
                "order": 1,
                "kind": "generate",
                "title": "Generate a patch",
                "description": "Create the smallest Kubernetes resource change.",
            },
            {
                "order": 2,
                "kind": "validate",
                "title": "Validate the patch",
                "description": "Run policy and schema checks in the sandbox.",
            },
            {
                "order": 3,
                "kind": "request_approval",
                "title": "Request review",
                "description": "Present the validated change for human approval.",
            },
        ],
    }


def test_fake_model_validates_and_records_requests() -> None:
    model = FakeStructuredModel([make_plan_payload()])

    plan = model.generate_structured(
        system_prompt="system",
        user_prompt="user",
        response_model=RemediationPlan,
    )

    assert plan.steps[-1].kind is PlanStepKind.REQUEST_APPROVAL
    assert model.requests[0].response_model == "RemediationPlan"


def test_fake_model_supports_json_and_exhaustion() -> None:
    model = FakeStructuredModel([json.dumps(make_plan_payload())])

    assert model.generate_structured(
        system_prompt="system", user_prompt="user", response_model=RemediationPlan
    ).summary
    with pytest.raises(ModelGatewayError, match="no queued response"):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_fake_model_wraps_invalid_schema_output() -> None:
    model = FakeStructuredModel([{"summary": "incomplete"}])

    with pytest.raises(ModelOutputError, match="schema validation"):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_fake_model_propagates_configured_gateway_error() -> None:
    model = FakeStructuredModel([ModelGatewayError("provider unavailable")])

    with pytest.raises(ModelGatewayError, match="provider unavailable"):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_hugging_face_gateway_sends_schema_without_live_call() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(make_plan_payload())}}]},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://router.huggingface.co/v1",
    )
    model = HuggingFaceStructuredModel(
        token="hf_test_token",
        model="openai/gpt-oss-20b:cheapest",
        max_output_tokens=1200,
        client=client,
    )

    plan = model.generate_structured(
        system_prompt="system", user_prompt="user", response_model=RemediationPlan
    )

    payload = captured["payload"]
    assert plan.summary.startswith("Increase")
    assert captured["authorization"] == "Bearer hf_test_token"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["temperature"] == 0
    assert payload["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"choices": []},
        {"choices": ["bad choice"]},
        {"choices": [{}]},
        {"choices": [{"message": {"content": ""}}]},
    ],
)
def test_hugging_face_gateway_rejects_malformed_responses(response: object) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        base_url="https://router.huggingface.co/v1",
    )
    model = HuggingFaceStructuredModel(
        token="hf_test_token",
        model="openai/gpt-oss-20b:cheapest",
        max_output_tokens=1200,
        client=client,
    )

    with pytest.raises(ModelOutputError):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_hugging_face_gateway_rejects_invalid_structured_content() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"summary":"bad"}'}}]}
            )
        ),
        base_url="https://router.huggingface.co/v1",
    )
    model = HuggingFaceStructuredModel(
        token="hf_test_token",
        model="openai/gpt-oss-20b:cheapest",
        max_output_tokens=1200,
        client=client,
    )

    with pytest.raises(ModelOutputError, match="schema validation"):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_hugging_face_gateway_rejects_http_errors_and_blank_token() -> None:
    with pytest.raises(ValueError, match="token"):
        HuggingFaceStructuredModel(token=" ", model="model", max_output_tokens=128)

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="https://router.huggingface.co/v1",
    )
    model = HuggingFaceStructuredModel(
        token="hf_test_token", model="model", max_output_tokens=128, client=client
    )
    with pytest.raises(ModelGatewayError):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_hugging_face_gateway_builds_from_settings() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="https://router.huggingface.co/v1",
    )
    settings = Settings(_env_file=None, hf_token="hf_test_token")

    model = HuggingFaceStructuredModel.from_settings(settings, client=client)

    assert isinstance(model, HuggingFaceStructuredModel)


def test_hugging_face_gateway_rejects_incompatible_settings() -> None:
    with pytest.raises(ValueError, match="do not select"):
        HuggingFaceStructuredModel.from_settings(
            Settings(_env_file=None, model_provider=ModelProvider.FAKE)
        )
    with pytest.raises(ValueError, match="HF_TOKEN"):
        HuggingFaceStructuredModel.from_settings(Settings(_env_file=None, hf_token=None))


def test_hugging_face_gateway_context_manager_closes_owned_client() -> None:
    model = HuggingFaceStructuredModel(token="hf_test_token", model="model", max_output_tokens=128)

    with model as entered:
        assert entered is model

    with pytest.raises(RuntimeError, match="client has been closed"):
        model.generate_structured(
            system_prompt="system", user_prompt="user", response_model=RemediationPlan
        )


def test_plan_contract_enforces_execution_order() -> None:
    payload = make_plan_payload()
    steps = [PlanStep.model_validate(item) for item in payload["steps"]]
    reordered = [
        steps[1].model_copy(update={"order": 1}),
        steps[0].model_copy(update={"order": 2}),
        steps[2],
    ]

    with pytest.raises(ValueError, match="generate, validate"):
        RemediationPlan(
            incident_id=uuid4(),
            summary="Unsafe order",
            steps=tuple(reordered),
        )
