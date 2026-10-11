"""Configuration loading and redaction tests."""

from agentic_incident_remediation.config import ModelProvider, Settings


def test_settings_load_hugging_face_token_without_exposing_it(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret_value")
    settings = Settings(_env_file=None)

    assert settings.model_provider is ModelProvider.HUGGINGFACE
    assert settings.live_model_configured is True
    assert "hf_secret_value" not in repr(settings)
    assert "hf_token" not in settings.safe_summary()


def test_settings_report_unconfigured_live_model(monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)

    settings = Settings(_env_file=None)

    assert settings.live_model_configured is False


def test_settings_accept_prefixed_overrides(monkeypatch) -> None:
    monkeypatch.setenv("AIR_MAX_REFLECTION_ATTEMPTS", "5")
    monkeypatch.setenv("AIR_MODEL_MAX_OUTPUT_TOKENS", "2048")

    settings = Settings(_env_file=None)

    assert settings.max_reflection_attempts == 5
    assert settings.model_max_output_tokens == 2048
