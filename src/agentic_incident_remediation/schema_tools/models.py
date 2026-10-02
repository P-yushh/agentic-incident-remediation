"""Typed contracts shared by schema MCP tools and their consumers."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from agentic_incident_remediation.models.base import StrictModel


class SchemaToolName(StrEnum):
    """Read-only schema tools exposed by the infrastructure MCP server."""

    GET_K8S_RESOURCE_SCHEMA = "get_k8s_resource_schema"
    GET_TERRAFORM_PROVIDER_DOCS = "get_terraform_provider_docs"


class AttributeRequirement(StrEnum):
    """How an infrastructure field is represented in its source schema."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    COMPUTED = "computed"


class SchemaField(StrictModel):
    """Relevant field extracted from an infrastructure schema snapshot."""

    path: str = Field(min_length=1, max_length=200)
    value_type: str = Field(min_length=1, max_length=100)
    requirement: AttributeRequirement
    description: str = Field(min_length=1, max_length=1000)
    allowed_values: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()


class KubernetesResourceSchema(StrictModel):
    """Curated OpenAPI field subset for one Kubernetes resource."""

    platform: Literal["kubernetes"] = "kubernetes"
    kind: str = Field(min_length=1, max_length=100)
    api_version: str = Field(min_length=1, max_length=100)
    schema_version: str = Field(min_length=1, max_length=100)
    fields: tuple[SchemaField, ...] = Field(min_length=1)
    safety_notes: tuple[str, ...] = ()
    source_url: str = Field(min_length=1, max_length=500)


class TerraformProviderDocs(StrictModel):
    """Curated provider-schema fields for one Terraform resource."""

    platform: Literal["terraform"] = "terraform"
    provider: str = Field(min_length=1, max_length=100)
    resource: str = Field(min_length=1, max_length=150)
    provider_version: str = Field(min_length=1, max_length=100)
    fields: tuple[SchemaField, ...] = Field(min_length=1)
    safety_notes: tuple[str, ...] = ()
    source_url: str = Field(min_length=1, max_length=500)


type SchemaDocument = KubernetesResourceSchema | TerraformProviderDocs


class SchemaToolRequest(StrictModel):
    """Validated request selected for one incident before tool execution."""

    name: SchemaToolName
    arguments: dict[str, str]


class SchemaToolDefinition(StrictModel):
    """Auditable MCP tool metadata returned during discovery."""

    name: SchemaToolName
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    read_only: bool = False
    destructive: bool = True
