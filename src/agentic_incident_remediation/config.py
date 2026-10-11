"""Environment-driven application configuration."""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Log levels accepted by the standard logging package."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ModelProvider(StrEnum):
    """Model providers understood by the application configuration."""

    HUGGINGFACE = "huggingface"
    FAKE = "fake"
    FIXTURE = "fixture"


class ReasoningEffort(StrEnum):
    """Portable reasoning-effort values supported by the first provider adapter."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Settings(BaseSettings):
    """Validated settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AIR_",
        case_sensitive=False,
        extra="ignore",
    )

    application_name: str = "agentic-incident-remediation"
    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO

    redis_url: str = "redis://localhost:6379/0"
    max_reflection_attempts: int = Field(default=3, ge=1, le=10)

    model_provider: ModelProvider = ModelProvider.HUGGINGFACE
    model_name: str = "openai/gpt-oss-20b:cheapest"
    model_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    model_max_output_tokens: int = Field(default=1200, ge=128, le=8192)
    hf_token: SecretStr | None = Field(default=None, validation_alias="HF_TOKEN")

    @property
    def live_model_configured(self) -> bool:
        """Return whether the selected live provider has its required credential."""

        if self.model_provider is ModelProvider.HUGGINGFACE:
            return self.hf_token is not None and bool(self.hf_token.get_secret_value())
        return True

    def safe_summary(self) -> dict[str, str | int | bool]:
        """Return configuration metadata that is safe to include in logs."""

        return {
            "application_name": self.application_name,
            "environment": self.environment.value,
            "log_level": self.log_level.value,
            "model_provider": self.model_provider.value,
            "model_name": self.model_name,
            "model_max_output_tokens": self.model_max_output_tokens,
            "live_model_configured": self.live_model_configured,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache process-wide settings."""

    return Settings()
