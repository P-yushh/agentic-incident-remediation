"""Incident-aware schema tool selection and local execution."""

from typing import Protocol

from agentic_incident_remediation.models import Incident, IncidentType
from agentic_incident_remediation.schema_tools.catalog import (
    get_k8s_resource_schema,
    get_terraform_provider_docs,
)
from agentic_incident_remediation.schema_tools.models import (
    KubernetesResourceSchema,
    SchemaDocument,
    SchemaToolName,
    SchemaToolRequest,
    TerraformProviderDocs,
)


class SchemaToolProvider(Protocol):
    """Synchronous tool boundary consumed by the current LangGraph generator node."""

    def execute(self, request: SchemaToolRequest) -> SchemaDocument:
        """Execute one approved read-only schema lookup."""


class LocalSchemaToolProvider:
    """Deterministic in-process adapter over the same catalog exposed through MCP."""

    def execute(self, request: SchemaToolRequest) -> SchemaDocument:
        if request.name is SchemaToolName.GET_K8S_RESOURCE_SCHEMA:
            return get_k8s_resource_schema(**request.arguments)
        if request.name is SchemaToolName.GET_TERRAFORM_PROVIDER_DOCS:
            return get_terraform_provider_docs(**request.arguments)
        raise AssertionError(f"unhandled schema tool: {request.name}")


def select_schema_tool(incident: Incident) -> SchemaToolRequest:
    """Choose a schema lookup from normalized incident type and approved context keys."""

    if incident.type in {
        IncidentType.KUBERNETES_OOM_KILLED,
        IncidentType.KUBERNETES_CRASH_LOOP,
    }:
        return SchemaToolRequest(
            name=SchemaToolName.GET_K8S_RESOURCE_SCHEMA,
            arguments={
                "kind": _context_string(incident, "k8s_kind", "Deployment"),
                "api_version": _context_string(incident, "k8s_api_version", "apps/v1"),
            },
        )
    return SchemaToolRequest(
        name=SchemaToolName.GET_TERRAFORM_PROVIDER_DOCS,
        arguments={
            "provider": _context_string(incident, "terraform_provider", "hashicorp/aws"),
            "resource": _context_string(incident, "terraform_resource", "aws_security_group_rule"),
        },
    )


def schema_context_for_incident(
    incident: Incident, provider: SchemaToolProvider
) -> KubernetesResourceSchema | TerraformProviderDocs:
    """Select and execute exactly one schema tool for an incident."""

    return provider.execute(select_schema_tool(incident))


def _context_string(incident: Incident, key: str, default: str) -> str:
    value = incident.context.get(key, incident.labels.get(key, default))
    return value if isinstance(value, str) and value.strip() else default
