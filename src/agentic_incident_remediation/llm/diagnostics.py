"""Allowlisted failure categories: never retain provider bodies, prompts or credentials."""

from typing import Literal

from agentic_incident_remediation.models.base import StrictModel

FailureCode = Literal[
    "provider_rate_limit",
    "provider_auth",
    "provider_unavailable",
    "provider_request",
    "provider_network",
    "model_output",
    "unknown",
]

FAILURE_MESSAGES: dict[FailureCode, str] = {
    "provider_rate_limit": (
        "AI provider quota or rate limit reached (HTTP 429). The provider rejected the request; "
        "daily quota exhaustion versus a temporary rate limit is not distinguished. "
        "No automatic retry was made."
    ),
    "provider_auth": (
        "The AI provider rejected authentication or access. "
        "Check the configured key and model access without exposing credentials."
    ),
    "provider_unavailable": "The AI provider returned a server error. No automatic retry was made.",
    "provider_request": (
        "The AI provider rejected the request. Check the model and request configuration."
    ),
    "provider_network": (
        "The AI provider request could not complete over the network. No automatic retry was made."
    ),
    "model_output": (
        "The AI response was incomplete, blocked, malformed or failed the required output schema."
    ),
    "unknown": (
        "GitHub execution failed. The runner did not record a specific cause; "
        "quota exhaustion is not confirmed. Inspect the failed job before retrying."
    ),
}


class ModelFailure(StrictModel):
    code: FailureCode


class ReportedModelFailure(RuntimeError):
    def __init__(self, diagnostic: ModelFailure) -> None:
        self.diagnostic = diagnostic
        super().__init__("model preparation failed; sanitized diagnostic available")
