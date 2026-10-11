"""Normalize external alert payloads into the workflow incident contract."""

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agentic_incident_remediation.models import Incident, IncidentSeverity, IncidentType


class IncidentNormalizationError(ValueError):
    """Raised when an alert cannot be mapped to a supported incident."""


_TYPE_ALIASES: dict[str, IncidentType] = {
    "kubernetes_oom_killed": IncidentType.KUBERNETES_OOM_KILLED,
    "kube_pod_oom_killed": IncidentType.KUBERNETES_OOM_KILLED,
    "kube_pod_container_oom_killed": IncidentType.KUBERNETES_OOM_KILLED,
    "oom_killed": IncidentType.KUBERNETES_OOM_KILLED,
    "oomkilled": IncidentType.KUBERNETES_OOM_KILLED,
    "kubernetes_crash_loop": IncidentType.KUBERNETES_CRASH_LOOP,
    "kube_pod_crash_looping": IncidentType.KUBERNETES_CRASH_LOOP,
    "crash_loop_back_off": IncidentType.KUBERNETES_CRASH_LOOP,
    "crashloopbackoff": IncidentType.KUBERNETES_CRASH_LOOP,
    "terraform_drift": IncidentType.TERRAFORM_DRIFT,
    "terraform_state_drift": IncidentType.TERRAFORM_DRIFT,
    "invalid_security_group_rule": IncidentType.INVALID_SECURITY_GROUP_RULE,
    "security_group_rule_invalid": IncidentType.INVALID_SECURITY_GROUP_RULE,
}

_SEVERITY_ALIASES: dict[str, IncidentSeverity] = {
    "p1": IncidentSeverity.CRITICAL,
    "sev1": IncidentSeverity.CRITICAL,
    "critical": IncidentSeverity.CRITICAL,
    "p2": IncidentSeverity.HIGH,
    "sev2": IncidentSeverity.HIGH,
    "high": IncidentSeverity.HIGH,
    "warning": IncidentSeverity.MEDIUM,
    "p3": IncidentSeverity.MEDIUM,
    "sev3": IncidentSeverity.MEDIUM,
    "medium": IncidentSeverity.MEDIUM,
    "info": IncidentSeverity.LOW,
    "p4": IncidentSeverity.LOW,
    "sev4": IncidentSeverity.LOW,
    "low": IncidentSeverity.LOW,
}

_CANONICAL_KEYS = {
    "annotations",
    "context",
    "description",
    "event",
    "id",
    "incident_type",
    "labels",
    "message",
    "occurred_at",
    "severity",
    "source",
    "startsAt",
    "summary",
    "timestamp",
    "title",
    "type",
}


def _slug(value: object) -> str:
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", str(value))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).strip().lower()
    return "_".join(
        part for part in "".join(char if char.isalnum() else " " for char in text).split()
    )


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _object_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _resolve_type(payload: Mapping[str, Any], labels: Mapping[str, str]) -> IncidentType:
    candidates = (
        payload.get("type"),
        payload.get("incident_type"),
        payload.get("event"),
        labels.get("alertname"),
        labels.get("reason"),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        key = _slug(candidate)
        if key in _TYPE_ALIASES:
            return _TYPE_ALIASES[key]
    raise IncidentNormalizationError("alert type is missing or unsupported")


def _resolve_severity(payload: Mapping[str, Any], labels: Mapping[str, str]) -> IncidentSeverity:
    raw = payload.get("severity", labels.get("severity", IncidentSeverity.MEDIUM.value))
    severity = _SEVERITY_ALIASES.get(_slug(raw))
    if severity is None:
        raise IncidentNormalizationError(f"unsupported incident severity: {raw}")
    return severity


def normalize_incident(alert: Incident | Mapping[str, Any]) -> Incident:
    """Convert a provider-shaped alert into a strict, provider-neutral incident."""

    if isinstance(alert, Incident):
        return alert
    if not isinstance(alert, Mapping):
        raise IncidentNormalizationError("alert must be an Incident or mapping")

    payload = {str(key): value for key, value in alert.items()}
    labels = _string_mapping(payload.get("labels"))
    annotations = _string_mapping(payload.get("annotations"))
    incident_type = _resolve_type(payload, labels)

    title = payload.get("title") or payload.get("summary") or annotations.get("summary")
    description = (
        payload.get("description")
        or payload.get("message")
        or annotations.get("description")
        or title
    )
    if not title or not description:
        raise IncidentNormalizationError("alert requires a title and description")

    context = _object_mapping(payload.get("context"))
    context.update({key: value for key, value in payload.items() if key not in _CANONICAL_KEYS})
    if annotations:
        context.setdefault("annotations", annotations)

    incident_data: dict[str, Any] = {
        "type": incident_type,
        "title": str(title),
        "description": str(description),
        "severity": _resolve_severity(payload, labels),
        "source": str(payload.get("source") or "alert-webhook"),
        "labels": labels,
        "context": context,
    }
    for source_key in ("occurred_at", "timestamp", "startsAt"):
        if payload.get(source_key) is not None:
            incident_data["occurred_at"] = payload[source_key]
            break
    if payload.get("id") is not None:
        incident_data["id"] = payload["id"]

    try:
        return Incident.model_validate(incident_data)
    except ValidationError as error:
        raise IncidentNormalizationError("alert fields failed incident validation") from error
