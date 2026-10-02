"""Typed client for MCP schema discovery and tool invocation."""

from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextContent

from agentic_incident_remediation.schema_tools.errors import SchemaMCPError
from agentic_incident_remediation.schema_tools.models import (
    KubernetesResourceSchema,
    SchemaDocument,
    SchemaToolDefinition,
    SchemaToolName,
    SchemaToolRequest,
    TerraformProviderDocs,
)


class SchemaMCPClient:
    """Connect to an in-memory or remote MCP schema server."""

    def __init__(self, target: MCPServer[Any] | str) -> None:
        self._target = target

    async def discover_tools(self) -> tuple[SchemaToolDefinition, ...]:
        """Discover and validate the schema server's available tools."""

        async with Client(self._target) as client:
            result = await client.list_tools()

        definitions: list[SchemaToolDefinition] = []
        for tool in result.tools:
            try:
                name = SchemaToolName(tool.name)
            except ValueError as error:
                raise SchemaMCPError(
                    f"schema server exposed unexpected tool: {tool.name}"
                ) from error
            annotations = tool.annotations
            definitions.append(
                SchemaToolDefinition(
                    name=name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                    read_only=bool(annotations and annotations.read_only_hint),
                    destructive=not bool(annotations and annotations.destructive_hint is False),
                )
            )
        return tuple(definitions)

    async def execute(self, request: SchemaToolRequest) -> SchemaDocument:
        """Execute one schema tool and validate its structured MCP response."""

        async with Client(self._target) as client:
            result = await client.call_tool(request.name.value, request.arguments)

        if result.is_error:
            detail = " ".join(
                block.text for block in result.content if isinstance(block, TextContent)
            )
            raise SchemaMCPError(detail or f"schema tool failed: {request.name.value}")
        if result.structured_content is None:
            raise SchemaMCPError(
                f"schema tool returned no structured content: {request.name.value}"
            )
        if request.name is SchemaToolName.GET_K8S_RESOURCE_SCHEMA:
            return KubernetesResourceSchema.model_validate(result.structured_content)
        return TerraformProviderDocs.model_validate(result.structured_content)
