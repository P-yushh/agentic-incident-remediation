"""Normalize validator-specific output into workflow validation contracts."""

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agentic_incident_remediation.models import (
    FindingSeverity,
    ValidationFinding,
    ValidationResult,
    ValidationStatus,
)
from agentic_incident_remediation.sandbox.models import SandboxExecution, ValidationTool


def normalize_validation_result(artifact_id: UUID, execution: SandboxExecution) -> ValidationResult:
    """Convert bounded process output into a deterministic validation result."""

    status = _validation_status(execution)
    findings = _parse_findings(execution)
    if status is not ValidationStatus.PASSED and not findings:
        findings = (
            ValidationFinding(
                tool=execution.tool.value,
                severity=(
                    FindingSeverity.CRITICAL
                    if status is ValidationStatus.ERROR
                    else FindingSeverity.ERROR
                ),
                message=(
                    execution.stderr.strip()
                    or execution.stdout.strip()
                    or f"{execution.tool.value} exited with code {execution.exit_code}"
                ),
                code="SANDBOX_ERROR" if status is ValidationStatus.ERROR else "VALIDATION_FAILED",
            ),
        )
    return ValidationResult(
        artifact_id=artifact_id,
        tool=execution.tool.value,
        status=status,
        exit_code=execution.exit_code,
        stdout=execution.stdout,
        stderr=execution.stderr,
        findings=findings,
        duration_ms=execution.duration_ms,
    )


def _validation_status(execution: SandboxExecution) -> ValidationStatus:
    if execution.timed_out or execution.exit_code in {125, 126, 127, 137, 143}:
        return ValidationStatus.ERROR
    if execution.exit_code == 0:
        return ValidationStatus.PASSED
    return ValidationStatus.FAILED


def _parse_findings(execution: SandboxExecution) -> tuple[ValidationFinding, ...]:
    try:
        payload: object = json.loads(execution.stdout)
    except (json.JSONDecodeError, TypeError):
        return ()
    if execution.tool is ValidationTool.KUBECONFORM:
        return _parse_kubeconform(payload)
    return _parse_terraform(payload)


def _parse_kubeconform(payload: object) -> tuple[ValidationFinding, ...]:
    if not isinstance(payload, Mapping):
        return ()
    resources = payload.get("resources")
    if not isinstance(resources, list):
        return ()
    findings: list[ValidationFinding] = []
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        status = resource.get("status")
        if status not in {"INVALID", "ERROR"}:
            continue
        findings.append(
            ValidationFinding(
                tool=ValidationTool.KUBECONFORM.value,
                severity=FindingSeverity.ERROR,
                message=_string(resource.get("msg"), "Kubernetes schema validation failed"),
                code=f"KUBECONFORM_{status}",
                path=_optional_string(resource.get("filename")),
            )
        )
    return tuple(findings)


def _parse_terraform(payload: object) -> tuple[ValidationFinding, ...]:
    if not isinstance(payload, Mapping):
        return ()
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list):
        return ()
    findings: list[ValidationFinding] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, Mapping):
            continue
        severity = (
            FindingSeverity.ERROR
            if diagnostic.get("severity") == "error"
            else FindingSeverity.WARNING
        )
        summary = _string(diagnostic.get("summary"), "Terraform validation finding")
        detail = _optional_string(diagnostic.get("detail"))
        source_range = diagnostic.get("range")
        path, line = _terraform_location(source_range)
        findings.append(
            ValidationFinding(
                tool=ValidationTool.TERRAFORM_VALIDATE.value,
                severity=severity,
                message=f"{summary}: {detail}" if detail else summary,
                code="TERRAFORM_VALIDATE",
                path=path,
                line=line,
            )
        )
    return tuple(findings)


def _terraform_location(source_range: object) -> tuple[str | None, int | None]:
    if not isinstance(source_range, Mapping):
        return None, None
    path = _optional_string(source_range.get("filename"))
    start = source_range.get("start")
    line: int | None = None
    if isinstance(start, Mapping):
        candidate = start.get("line")
        if isinstance(candidate, int) and candidate >= 1:
            line = candidate
    return path, line


def _string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
