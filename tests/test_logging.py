"""Structured logging tests."""

import json
import logging

from agentic_incident_remediation.logging import JsonFormatter


def test_json_formatter_emits_context_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="remediation.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=12,
        msg="incident received",
        args=(),
        exc_info=None,
    )
    record.execution_id = "execution-123"

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "incident received"
    assert payload["execution_id"] == "execution-123"
    assert payload["timestamp"].endswith("+00:00")
