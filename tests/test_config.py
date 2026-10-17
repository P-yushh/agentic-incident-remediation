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


def test_settings_select_gemini_without_exposing_its_key(monkeypatch) -> None:
    monkeypatch.setenv("AIR_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-secret")
    settings = Settings(_env_file=None)

    assert settings.model_provider is ModelProvider.GEMINI
    assert settings.model_name == "gemini-3.8-flash"
    assert settings.live_model_configured is True
    assert "synthetic-gemini-secret" not in repr(settings)
    assert "gemini_api_key" not in settings.safe_summary()
    assert settings.safe_summary()["model_name"] == "gemini-3.8-flash"


def test_gemini_key_does_not_configure_hugging_face_or_vice_versa() -> None:
    gemini = Settings(
        _env_file=None, model_provider="gemini", gemini_api_key=None, hf_token="hf_test"
    )
    hf = Settings(_env_file=None, gemini_api_key="synthetic-key", hf_token=None)
    blank = Settings(_env_file=None, model_provider="gemini", gemini_api_key="  ")

    assert not gemini.live_model_configured
    assert not hf.live_model_configured
    assert not blank.live_model_configured


def test_gemini_model_can_be_overridden_without_changing_hugging_face_default() -> None:
    assert Settings(_env_file=None).model_name == "openai/gpt-oss-20b:cheapest"
    settings = Settings(_env_file=None, model_provider="gemini", model_name="gemini-2.5-flash")
    assert settings.model_name == "gemini-2.5-flash"
