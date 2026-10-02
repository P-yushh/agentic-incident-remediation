"""Read-only MCP server for approved infrastructure schema lookups."""

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from agentic_incident_remediation.schema_tools.catalog import (
    get_k8s_resource_schema as lookup_k8s_resource_schema,
)
from agentic_incident_remediation.schema_tools.catalog import (
    get_terraform_provider_docs as lookup_terraform_provider_docs,
)
from agentic_incident_remediation.schema_tools.errors import SchemaLookupError
from agentic_incident_remediation.schema_tools.models import (
    KubernetesResourceSchema,
    TerraformProviderDocs,
)

READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

schema_mcp_server = MCPServer(
    name="infrastructure-schema",
    title="Infrastructure Schema Tools",
    description="Read-only Kubernetes and Terraform schema snapshots for artifact generation.",
    instructions=(
        "Use these tools only to inspect approved schema metadata. They never access a cluster, "
        "cloud account, filesystem, or credentials."
    ),
    version="0.1.0",
)


@schema_mcp_server.tool(
    name="get_k8s_resource_schema",
    description="Return an approved Kubernetes OpenAPI field subset for a resource.",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def get_k8s_resource_schema(kind: str, api_version: str) -> KubernetesResourceSchema:
    """Fetch schema fields needed to generate a Kubernetes resource safely."""

    try:
        return lookup_k8s_resource_schema(kind, api_version)
    except SchemaLookupError as error:
        raise ToolError(str(error)) from error


@schema_mcp_server.tool(
    name="get_terraform_provider_docs",
    description="Return approved Terraform provider fields for a resource.",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def get_terraform_provider_docs(provider: str, resource: str) -> TerraformProviderDocs:
    """Fetch provider fields needed to generate a Terraform resource safely."""

    try:
        return lookup_terraform_provider_docs(provider, resource)
    except SchemaLookupError as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Run the local schema server over MCP stdio transport."""

    schema_mcp_server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
