"""Gemini Developer API structured generation without SDK retries or tool execution."""

import re
from collections.abc import Mapping

import httpx
from pydantic import ValidationError

from agentic_incident_remediation.config import ModelProvider, ReasoningEffort, Settings
from agentic_incident_remediation.llm.diagnostics import ModelFailure
from agentic_incident_remediation.llm.gateway import (
    ModelGatewayError,
    ModelOutputError,
    StructuredResponse,
)


class GeminiStructuredModel:
    """Use the native Gemini API; credentials go in a header, never a URL."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int,
        reasoning_effort: ReasoningEffort = ReasoningEffort.LOW,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("a Gemini API key is required")
        if not re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]*", model):
            raise ValueError("Gemini model must be a bare gemini-* model ID")
        if not 128 <= max_output_tokens <= 8192:
            raise ValueError("Gemini output token limit must be between 128 and 8192")
        self._api_key = api_key.strip()
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._client = client or httpx.Client(
            base_url="https://generativelanguage.googleapis.com/v1beta/", timeout=30.0
        )
        self._owns_client = client is None
        self.last_failure: ModelFailure | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, client: httpx.Client | None = None
    ) -> "GeminiStructuredModel":
        """Require explicit Gemini selection and its own credential."""

        if settings.model_provider is not ModelProvider.GEMINI:
            raise ValueError("settings do not select the Gemini provider")
        if settings.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required for live Gemini inference")
        return cls(
            api_key=settings.gemini_api_key.get_secret_value(),
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
        """Make one bounded request and validate the final JSON locally."""

        self.last_failure = None

        generation_config: dict[str, object] = {
            "temperature": 0,
            "candidateCount": 1,
            "maxOutputTokens": self._max_output_tokens,
            "responseMimeType": "application/json",
            "responseJsonSchema": response_model.model_json_schema(),
        }
        if self._model.startswith(("gemini-3", "gemini-4")):
            generation_config["thinkingConfig"] = {
                "thinkingLevel": self._reasoning_effort.value.upper(),
                "includeThoughts": False,
            }
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }
        try:
            response = self._client.post(
                f"models/{self._model}:generateContent",
                headers={"x-goog-api-key": self._api_key},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            self.last_failure = ModelFailure(
                code=(
                    "provider_rate_limit"
                    if status == 429
                    else "provider_auth"
                    if status in {401, 403}
                    else "provider_unavailable"
                    if status >= 500
                    else "provider_request"
                )
            )
            # Never echo provider bodies, request headers, prompts, or credential-bearing errors.
            raise ModelGatewayError(
                f"Gemini request failed (HTTP {error.response.status_code}); no retry attempted"
            ) from None
        except httpx.HTTPError:
            self.last_failure = ModelFailure(code="provider_network")
            raise ModelGatewayError("Gemini request failed; no retry attempted") from None

        try:
            content = _extract_gemini_content(response.json())
            return response_model.model_validate_json(content)
        except ValidationError:
            self.last_failure = ModelFailure(code="model_output")
            raise ModelOutputError("Gemini output failed schema validation") from None
        except ModelOutputError:
            self.last_failure = ModelFailure(code="model_output")
            raise
        except ValueError:
            self.last_failure = ModelFailure(code="model_output")
            raise ModelOutputError("Gemini response is not valid JSON") from None

    def close(self) -> None:
        """Close only the HTTP client owned by this gateway."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GeminiStructuredModel":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _extract_gemini_content(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise ModelOutputError("Gemini response is not a JSON object")
    feedback = payload.get("promptFeedback", {})
    if not isinstance(feedback, Mapping) or feedback.get("blockReason") not in (
        None,
        "BLOCK_REASON_UNSPECIFIED",
    ):
        raise ModelOutputError("Gemini blocked the prompt")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ModelOutputError("Gemini response must contain one candidate")
    candidate = candidates[0]
    if not isinstance(candidate, Mapping) or candidate.get("finishReason") != "STOP":
        raise ModelOutputError("Gemini response was blocked, truncated, or incomplete")
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        raise ModelOutputError("Gemini response has no content")
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ModelOutputError("Gemini response has no text parts")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            raise ModelOutputError("Gemini response contains a malformed part")
        if part.get("thought") is True:
            continue
        text = part.get("text")
        if not isinstance(text, str):
            raise ModelOutputError("Gemini response contains a non-text part")
        texts.append(text)
    result = "".join(texts)
    if not result.strip():
        raise ModelOutputError("Gemini response has no final text")
    return result
