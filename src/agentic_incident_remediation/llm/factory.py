"""Explicit live provider selection; never fall back to a different hosted service."""

import httpx

from agentic_incident_remediation.config import ModelProvider, Settings
from agentic_incident_remediation.llm.gateway import HuggingFaceStructuredModel
from agentic_incident_remediation.llm.gemini import GeminiStructuredModel


def create_live_model(
    settings: Settings, *, client: httpx.Client | None = None
) -> HuggingFaceStructuredModel | GeminiStructuredModel:
    """Select one configured live provider; offline demos use their existing factory."""

    if settings.model_provider is ModelProvider.GEMINI:
        return GeminiStructuredModel.from_settings(settings, client=client)
    if settings.model_provider is ModelProvider.HUGGINGFACE:
        return HuggingFaceStructuredModel.from_settings(settings, client=client)
    raise ValueError("live runtime requires the gemini or huggingface model provider")
