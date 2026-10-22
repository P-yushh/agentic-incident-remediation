"""Gemini request, response, and provider isolation tests with no hosted calls."""

import json
import traceback
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from agentic_incident_remediation.config import ModelProvider, ReasoningEffort, Settings
from agentic_incident_remediation.llm import (
    GeminiStructuredModel,
    HuggingFaceStructuredModel,
    ModelGatewayError,
    ModelOutputError,
    create_live_model,
)
from agentic_incident_remediation.models import ArtifactDraft, RemediationPlan

TEST_KEY = "synthetic-gemini-key-not-a-real-credential"
MODEL = "gemini-3.8-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/"


def plan_payload() -> dict[str, Any]:
    return {
        "incident_id": str(uuid4()),
        "summary": "Propose a validated correction for human review.",
        "steps": [
            {"order": n, "kind": kind, "title": kind, "description": kind}
            for n, kind in enumerate(("generate", "validate", "request_approval"), start=1)
        ],
    }


def envelope(text: str, *, reason: str = "STOP") -> dict[str, Any]:
    return {"candidates": [{"finishReason": reason, "content": {"parts": [{"text": text}]}}]}


def model_for(payload: object) -> tuple[GeminiStructuredModel, httpx.Client]:
    client = httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    return GeminiStructuredModel(
        api_key=TEST_KEY, model=MODEL, max_output_tokens=1200, client=client
    ), client


def generate(model: GeminiStructuredModel) -> RemediationPlan:
    return model.generate_structured(
        system_prompt="system", user_prompt="synthetic incident", response_model=RemediationPlan
    )


def test_gemini_sends_native_schema_bounded_request_and_header_only_credential() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=envelope(json.dumps(plan_payload())))

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        model = GeminiStructuredModel(
            api_key=TEST_KEY, model=MODEL, max_output_tokens=1200, client=client
        )
        assert generate(model).summary

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == BASE_URL + f"models/{MODEL}:generateContent"
    assert not request.url.query
    assert TEST_KEY not in str(request.url)
    assert TEST_KEY not in request.content.decode()
    assert request.headers["x-goog-api-key"] == TEST_KEY
    payload = json.loads(request.content)
    assert payload["systemInstruction"] == {"parts": [{"text": "system"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "synthetic incident"}]}]
    config = payload["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == RemediationPlan.model_json_schema()
    assert config["candidateCount"] == 1
    assert config["maxOutputTokens"] == 1200
    assert config["thinkingConfig"] == {"thinkingLevel": "LOW", "includeThoughts": False}
    assert "tools" not in payload


@pytest.mark.parametrize("legacy_model", ["gemini-2.5-flash", "gemini-2.5-flash-lite"])
def test_gemini_does_not_send_unsupported_thinking_level_to_older_models(legacy_model: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "thinkingConfig" not in json.loads(request.content)["generationConfig"]
        return httpx.Response(200, json=envelope(json.dumps(plan_payload())))

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        model = GeminiStructuredModel(
            api_key=TEST_KEY, model=legacy_model, max_output_tokens=1200, client=client
        )
        assert generate(model).summary


def test_gemini_joins_final_text_parts_and_ignores_thought_parts() -> None:
    text = json.dumps(plan_payload())
    response = envelope(text)
    response["candidates"][0]["content"]["parts"] = [
        {"thought": True, "text": "not final output"},
        {"text": text[:20]},
        {"text": text[20:]},
    ]
    model, client = model_for(response)
    with client:
        assert generate(model).summary


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"promptFeedback": []},
        {"promptFeedback": {"blockReason": []}},
        {"candidates": []},
        {"candidates": [{}, {}]},
        {"candidates": ["malformed"]},
        {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
        {"candidates": [{"finishReason": "STOP"}]},
        {"candidates": [{"finishReason": "STOP", "content": {}}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": [None]}}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": 5}]}}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {}}]}}]},
        {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "{}"}]}}
            ]
        },
        envelope(""),
        envelope("{}", reason="MAX_TOKENS"),
        envelope("{}", reason="SAFETY"),
    ],
)
def test_gemini_rejects_malformed_blocked_and_incomplete_responses(payload: object) -> None:
    model, client = model_for(payload)
    with client, pytest.raises(ModelOutputError):
        generate(model)


@pytest.mark.parametrize("text", ["not json", "{}", '{"summary":"incomplete"}'])
def test_gemini_validates_json_and_domain_contracts(text: str) -> None:
    model, client = model_for(envelope(text))
    with client, pytest.raises(ModelOutputError, match="schema validation"):
        generate(model)


def test_gemini_rejects_invalid_http_json() -> None:
    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="invalid json")),
    ) as client:
        model = GeminiStructuredModel(
            api_key=TEST_KEY, model=MODEL, max_output_tokens=1200, client=client
        )
        with pytest.raises(ModelOutputError, match="not valid JSON"):
            generate(model)


@pytest.mark.parametrize(
    ("path", "content"),
    [("../unsafe.yaml", "apiVersion: v1"), ("remediations/unsafe.yaml", "kubectl apply -f x")],
)
def test_gemini_artifacts_keep_existing_safety_guards(path: str, content: str) -> None:
    draft = {
        "kind": "kubernetes_manifest",
        "path": path,
        "content": content,
        "change_summary": "Synthetic unsafe proposal",
    }
    model, client = model_for(envelope(json.dumps(draft)))
    with client, pytest.raises(ModelOutputError, match="schema validation"):
        model.generate_structured(system_prompt="s", user_prompt="u", response_model=ArtifactDraft)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429, 500, 503])
def test_gemini_http_failures_do_not_retry_or_expose_secrets(status: int) -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"error": {"message": TEST_KEY}})

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        model = GeminiStructuredModel(
            api_key=TEST_KEY, model=MODEL, max_output_tokens=1200, client=client
        )
        with pytest.raises(ModelGatewayError, match=f"HTTP {status}") as caught:
            generate(model)
    assert attempts == 1
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
    assert model.last_failure is not None
    assert model.last_failure.code == (
        "provider_rate_limit"
        if status == 429
        else "provider_auth"
        if status in {401, 403}
        else "provider_unavailable"
        if status >= 500
        else "provider_request"
    )
    assert TEST_KEY not in model.last_failure.model_dump_json()


def test_gemini_transport_errors_are_sanitized_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(TEST_KEY, request=request)

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        model = GeminiStructuredModel(
            api_key=TEST_KEY, model=MODEL, max_output_tokens=1200, client=client
        )
        with pytest.raises(ModelGatewayError, match="no retry") as caught:
            generate(model)
    assert attempts == 1
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("name", ["", "models/gemini-3.8-flash", "../gemini", "gemini-x?key=x"])
def test_gemini_rejects_model_path_and_query_injection(name: str) -> None:
    with pytest.raises(ValueError, match="model"):
        GeminiStructuredModel(api_key=TEST_KEY, model=name, max_output_tokens=1200)


@pytest.mark.parametrize("tokens", [0, 127, 8193])
def test_gemini_rejects_unbounded_output_limits(tokens: int) -> None:
    with pytest.raises(ValueError, match="token limit"):
        GeminiStructuredModel(api_key=TEST_KEY, model=MODEL, max_output_tokens=tokens)


def test_gemini_requires_a_nonblank_key_and_explicit_provider() -> None:
    with pytest.raises(ValueError, match="API key"):
        GeminiStructuredModel(api_key=" ", model=MODEL, max_output_tokens=128)
    with pytest.raises(ValueError, match="do not select"):
        GeminiStructuredModel.from_settings(Settings(_env_file=None))
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiStructuredModel.from_settings(
            Settings(_env_file=None, model_provider="gemini", gemini_api_key=None)
        )


def test_gemini_closes_only_owned_clients() -> None:
    owned = GeminiStructuredModel(api_key=TEST_KEY, model=MODEL, max_output_tokens=128)
    with owned as entered:
        assert entered is owned
    assert owned._client.is_closed
    model, client = model_for(envelope(json.dumps(plan_payload())))
    with client:
        with model:
            pass
        assert not client.is_closed
        assert generate(model).summary


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (ModelProvider.GEMINI, GeminiStructuredModel),
        (ModelProvider.HUGGINGFACE, HuggingFaceStructuredModel),
    ],
)
def test_live_factory_selects_exactly_one_provider(provider: ModelProvider, expected: type) -> None:
    settings = Settings(
        _env_file=None, model_provider=provider, gemini_api_key=TEST_KEY, hf_token="hf_synthetic"
    )
    with httpx.Client(
        base_url=BASE_URL, transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        assert isinstance(create_live_model(settings, client=client), expected)


def test_live_factory_never_falls_back_from_gemini_to_hugging_face() -> None:
    settings = Settings(_env_file=None, model_provider="gemini", gemini_api_key=None)
    with patch.object(HuggingFaceStructuredModel, "from_settings") as hugging_face:
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            create_live_model(settings)
        hugging_face.assert_not_called()


@pytest.mark.parametrize("provider", [ModelProvider.FAKE, ModelProvider.FIXTURE])
def test_live_factory_rejects_offline_modes(provider: ModelProvider) -> None:
    with pytest.raises(ValueError, match="live runtime"):
        create_live_model(Settings(_env_file=None, model_provider=provider))


def test_gemini_from_settings_honors_configuration() -> None:
    settings = Settings(
        _env_file=None,
        model_provider="gemini",
        gemini_api_key=TEST_KEY,
        model_max_output_tokens=2048,
        model_reasoning_effort=ReasoningEffort.HIGH,
    )
    with GeminiStructuredModel.from_settings(settings) as model:
        assert model._model == MODEL
        assert model._max_output_tokens == 2048
        assert model._reasoning_effort is ReasoningEffort.HIGH
