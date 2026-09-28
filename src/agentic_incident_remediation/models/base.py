"""Shared model configuration and time helpers."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(tz=UTC)


class StrictModel(BaseModel):
    """Immutable base model that rejects unknown input fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
