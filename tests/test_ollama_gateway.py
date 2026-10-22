"""Loopback-only structured inference contracts; no live model calls."""

import json

import httpx
import pytest
from pydantic import BaseModel

from agentic_incident_remediation.llm.gateway import ModelGatewayError, ModelOutputError
from agentic_incident_remediation.llm.ollama import OllamaStructuredModel


class Reply(BaseModel):
    answer: int


def test_local_structured_request_and_metrics():
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["format"] == Reply.model_json_schema()
        assert body["think"] is False and body["stream"] is False
        assert body["options"] == {
            "temperature": 0,
            "seed": 3,
            "num_predict": 1200,
            "num_ctx": 8192,
        }
        return httpx.Response(
            200,
            json={
                "done": True,
                "done_reason": "stop",
                "eval_count": 12,
                "message": {"content": '{"answer":42}', "thinking": "not recorded"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with OllamaStructuredModel(client=client, seed=3) as model:
            reply = model.generate_structured(
                system_prompt="system", user_prompt="user", response_model=Reply
            )
            assert reply.answer == 42
            assert model.calls[0]["success"] is True
            assert model.calls[0]["eval_count"] == 12
            assert "thinking" not in str(model.calls)
        assert not client.is_closed


@pytest.mark.parametrize(
    "payload",
    [
        {"done": False},
        {"done": True, "done_reason": "length", "message": {"content": "{}"}},
        {"done": True, "done_reason": "stop", "message": {"content": '{"answer":"bad"}'}},
        {"done": True, "done_reason": "stop", "message": {"content": ""}},
        {"done": True, "done_reason": "stop", "message": {"content": "x" * 200001}},
        [],
        {"done": True, "done_reason": "stop", "message": None},
    ],
)
def test_invalid_or_incomplete_response_fails_closed(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as c:
        model = OllamaStructuredModel(client=c)
        with pytest.raises(ModelOutputError):
            model.generate_structured(system_prompt="s", user_prompt="u", response_model=Reply)
        assert len(model.calls) == 1 and model.calls[0]["success"] is False


@pytest.mark.parametrize("status", [302, 429, 500])
def test_http_errors_do_not_retry_redirect_or_expose_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private response", headers={"location": "https://bad"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        model = OllamaStructuredModel(client=c)
        with pytest.raises(ModelGatewayError, match=f"HTTP {status}") as error:
            model.generate_structured(system_prompt="s", user_prompt="u", response_model=Reply)
        assert "private" not in str(error.value)
        assert len(calls) == 1


def test_network_failure_has_no_retry():
    def handler(request):
        raise httpx.ReadTimeout("private", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        model = OllamaStructuredModel(client=c)
        with pytest.raises(ModelGatewayError, match="no retry attempted"):
            model.generate_structured(system_prompt="s", user_prompt="u", response_model=Reply)
        assert len(model.calls) == 1


@pytest.mark.parametrize(
    "kwargs", [{"model": "model:cloud"}, {"model": "bad model"}, {"max_output_tokens": 1}]
)
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        OllamaStructuredModel(**kwargs)


def test_owned_client_closed():
    with OllamaStructuredModel() as model:
        assert not model._client.is_closed
    assert model._client.is_closed
