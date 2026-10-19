"""Explicit loopback-only Ollama adapter for isolated evaluation; no hosted fallback."""

import re
from time import perf_counter

import httpx
from pydantic import ValidationError

from agentic_incident_remediation.llm.gateway import (
    ModelGatewayError,
    ModelOutputError,
    StructuredResponse,
)


class OllamaStructuredModel:
    """Use an already installed local model with the same Pydantic contracts as hosted models."""

    def __init__(
        self,
        *,
        model: str = "qwen3:8b",
        max_output_tokens: int = 1200,
        seed: int = 0,
        timeout_seconds: int = 90,
        context_tokens: int = 8192,
        client: httpx.Client | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model) or "cloud" in model.lower():
            raise ValueError("an installed local model name is required")
        if not 128 <= max_output_tokens <= 8192:
            raise ValueError("output token limit must be between 128 and 8192")
        if not 10 <= timeout_seconds <= 1200 or context_tokens not in {8192, 16384}:
            raise ValueError("invalid local inference time or context budget")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.seed = seed
        self.context_tokens = context_tokens
        self._client = client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        self._owns_client = client is None
        self.calls: list[dict[str, str | int | float | bool]] = []
        self.last_content = ""

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredResponse],
    ) -> StructuredResponse:
        started = perf_counter()
        call: dict[str, str | int | float | bool] = {
            "response_model": response_model.__name__,
            "success": False,
        }
        self.last_content = ""
        try:
            response = self._client.post(
                "http://127.0.0.1:11434/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "format": response_model.model_json_schema(),
                    "stream": False,
                    "think": False,
                    "keep_alive": "5m",
                    "options": {
                        "temperature": 0,
                        "seed": self.seed,
                        "num_predict": self.max_output_tokens,
                        "num_ctx": self.context_tokens,
                    },
                },
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise ModelGatewayError(f"Ollama request failed (HTTP {response.status_code})")
            payload = response.json()
            for key in ("eval_count", "prompt_eval_count", "total_duration", "load_duration"):
                if type(payload.get(key)) is int:
                    call[key] = payload[key]
            call["done_reason"] = str(payload.get("done_reason", "unknown"))[:40]
            content = payload.get("message", {}).get("content")
            if isinstance(content, str):
                self.last_content = content[:200_000]
            if payload.get("done") is not True or payload.get("done_reason") != "stop":
                raise ModelOutputError("Ollama output was incomplete or token-limited")
            if not isinstance(content, str) or not content.strip() or len(content) > 200_000:
                raise ModelOutputError("Ollama response has no bounded final text")
            result = response_model.model_validate_json(content)
            call["success"] = True
            return result
        except httpx.HTTPError:
            raise ModelGatewayError("Ollama could not be reached; no retry attempted") from None
        except (ValidationError, ValueError, AttributeError, TypeError):
            raise ModelOutputError("Ollama output failed schema validation") from None
        finally:
            call["wall_seconds"] = round(perf_counter() - started, 3)
            self.calls.append(call)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OllamaStructuredModel":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
