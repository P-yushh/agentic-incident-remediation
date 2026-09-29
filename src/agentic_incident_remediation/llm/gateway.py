"""Structured-output model abstraction and Hugging Face implementation."""

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from agentic_incident_remediation.config import ModelProvider, ReasoningEffort, Settings
from agentic_incident_remediation.models.base import StrictModel

StructuredResponse = TypeVar("StructuredResponse", bound=BaseModel)


class ModelGatewayError(RuntimeError):
    """Base error raised when a model request cannot produce a usable response."""


class ModelOutputError(ModelGatewayError):
    """Raised when model output does not satisfy the requested response schema."""


class ModelRequest(StrictModel):
    """Auditable provider-neutral description of one structured model request."""

    system_prompt: str
    user_prompt: str
    response_model: str


class StructuredModel(Protocol):
    """Minimal interface required by nodes that consume structured model output."""

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        """Return a response validated against ``response_model``."""


class FakeStructuredModel:
    """Deterministic queued model used by unit tests and offline demonstrations."""

    def __init__(self, responses: Sequence[object]) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        self.requests.append(
            ModelRequest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=response_model.__name__,
            )
        )
        if not self._responses:
            raise ModelGatewayError("fake model has no queued response")

        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response

        try:
            if isinstance(response, str):
                return response_model.model_validate_json(response)
            return response_model.model_validate(response)
        except ValidationError as error:
            raise ModelOutputError("model output failed schema validation") from error


class HuggingFaceStructuredModel:
    """Call Hugging Face's OpenAI-compatible router with a strict JSON schema."""

    def __init__(
        self,
        *,
        token: str,
        model: str,
        max_output_tokens: int,
        reasoning_effort: ReasoningEffort = ReasoningEffort.LOW,
        client: httpx.Client | None = None,
        base_url: str = "https://router.huggingface.co/v1",
    ) -> None:
        if not token.strip():
            raise ValueError("a Hugging Face token is required")
        self._token = token
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._client = client or httpx.Client(base_url=base_url, timeout=30.0)
        self._owns_client = client is None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, client: httpx.Client | None = None
    ) -> "HuggingFaceStructuredModel":
        """Build a live gateway from validated application settings."""

        if settings.model_provider is not ModelProvider.HUGGINGFACE:
            raise ValueError("settings do not select the Hugging Face provider")
        if settings.hf_token is None or not settings.hf_token.get_secret_value():
            raise ValueError("HF_TOKEN is required for live Hugging Face inference")
        return cls(
            token=settings.hf_token.get_secret_value(),
            model=settings.model_name,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
            client=client,
        )

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        schema = response_model.model_json_schema()
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._max_output_tokens,
            "reasoning_effort": self._reasoning_effort.value,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
        }

        try:
            response = self._client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
            )
            response.raise_for_status()
            content = _extract_message_content(response.json())
            return response_model.model_validate_json(content)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError) as error:
            if isinstance(error, ValidationError):
                raise ModelOutputError("Hugging Face output failed schema validation") from error
            raise ModelGatewayError("Hugging Face structured generation failed") from error

    def close(self) -> None:
        """Close the internally-created HTTP client."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HuggingFaceStructuredModel":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _extract_message_content(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise ModelOutputError("model response is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelOutputError("model response has no choices")
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise ModelOutputError("model choice is malformed")
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        raise ModelOutputError("model choice has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelOutputError("model message has no text content")
    return content
