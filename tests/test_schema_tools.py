"""Schema catalog, selection, and MCP transport tests."""

import asyncio
from collections.abc import Callable

import pytest

from agentic_incident_remediation.models import Incident, IncidentType
from agentic_incident_remediation.schema_tools import (
    AttributeRequirement,
    LocalSchemaToolProvider,
    SchemaLookupError,
    SchemaMCPClient,
    SchemaMCPError,
    SchemaToolName,
    SchemaToolRequest,
    get_k8s_resource_schema,
    get_terraform_provider_docs,
    schema_context_for_incident,
    schema_mcp_server,
    select_schema_tool,
)


def make_incident(incident_type: IncidentType, **context: str) -> Incident:
    return Incident(
        type=incident_type,
        title="Synthetic infrastructure incident",
        description="A fixture incident used to select a schema tool.",
        context=context,
    )


def test_kubernetes_catalog_returns_curated_deployment_schema() -> None:
    schema = get_k8s_resource_schema("Deployment", "apps/v1")

    assert schema.platform == "kubernetes"
    assert schema.kind == "Deployment"
    assert any(
        field.path.endswith("resources.limits.memory")
        and field.requirement is AttributeRequirement.OPTIONAL
        for field in schema.fields
    )
    assert all("apply" not in note.lower() for note in schema.safety_notes[:1])


def test_terraform_catalog_returns_security_group_constraints() -> None:
    docs = get_terraform_provider_docs("hashicorp/aws", "aws_security_group_rule")

    assert docs.platform == "terraform"
    assert docs.provider == "hashicorp/aws"
    cidr = next(field for field in docs.fields if field.path == "cidr_blocks")
    assert cidr.requirement is AttributeRequirement.OPTIONAL
    assert cidr.conflicts_with == ("source_security_group_id",)
    assert any("0.0.0.0/0" in note for note in docs.safety_notes)


@pytest.mark.parametrize(
    ("lookup", "arguments"),
    [
        (get_k8s_resource_schema, ("StatefulSet", "apps/v1")),
        (get_k8s_resource_schema, ("../Deployment", "apps/v1")),
        (get_terraform_provider_docs, ("hashicorp/google", "google_compute_instance")),
        (get_terraform_provider_docs, ("hashicorp/aws", "../../secret")),
    ],
)
def test_catalog_fails_closed_for_unknown_or_malformed_queries(
    lookup: Callable[[str, str], object], arguments: tuple[str, str]
) -> None:
    with pytest.raises(SchemaLookupError):
        lookup(*arguments)


@pytest.mark.parametrize(
    ("incident_type", "expected_tool"),
    [
        (IncidentType.KUBERNETES_OOM_KILLED, SchemaToolName.GET_K8S_RESOURCE_SCHEMA),
        (IncidentType.KUBERNETES_CRASH_LOOP, SchemaToolName.GET_K8S_RESOURCE_SCHEMA),
        (IncidentType.TERRAFORM_DRIFT, SchemaToolName.GET_TERRAFORM_PROVIDER_DOCS),
        (
            IncidentType.INVALID_SECURITY_GROUP_RULE,
            SchemaToolName.GET_TERRAFORM_PROVIDER_DOCS,
        ),
    ],
)
def test_incident_type_selects_schema_tool(
    incident_type: IncidentType, expected_tool: SchemaToolName
) -> None:
    request = select_schema_tool(make_incident(incident_type))

    assert request.name is expected_tool


def test_selection_uses_approved_incident_context_keys() -> None:
    incident = make_incident(
        IncidentType.KUBERNETES_OOM_KILLED,
        k8s_kind="Deployment",
        k8s_api_version="apps/v1",
    )

    request = select_schema_tool(incident)
    document = schema_context_for_incident(incident, LocalSchemaToolProvider())

    assert request.arguments == {"kind": "Deployment", "api_version": "apps/v1"}
    assert document.platform == "kubernetes"


def test_local_provider_executes_both_tools() -> None:
    provider = LocalSchemaToolProvider()

    kubernetes = provider.execute(
        SchemaToolRequest(
            name=SchemaToolName.GET_K8S_RESOURCE_SCHEMA,
            arguments={"kind": "Deployment", "api_version": "apps/v1"},
        )
    )
    terraform = provider.execute(
        SchemaToolRequest(
            name=SchemaToolName.GET_TERRAFORM_PROVIDER_DOCS,
            arguments={
                "provider": "hashicorp/aws",
                "resource": "aws_security_group_rule",
            },
        )
    )

    assert kubernetes.platform == "kubernetes"
    assert terraform.platform == "terraform"


def test_mcp_client_discovers_read_only_tools_and_calls_them() -> None:
    async def scenario() -> None:
        client = SchemaMCPClient(schema_mcp_server)

        tools = await client.discover_tools()
        assert {tool.name for tool in tools} == set(SchemaToolName)
        assert all(tool.read_only and not tool.destructive for tool in tools)
        assert all(tool.input_schema and tool.output_schema for tool in tools)

        document = await client.execute(
            SchemaToolRequest(
                name=SchemaToolName.GET_K8S_RESOURCE_SCHEMA,
                arguments={"kind": "Deployment", "api_version": "apps/v1"},
            )
        )
        assert document.platform == "kubernetes"

    asyncio.run(scenario())


def test_mcp_client_surfaces_tool_errors() -> None:
    async def scenario() -> None:
        client = SchemaMCPClient(schema_mcp_server)
        with pytest.raises(SchemaMCPError, match="unsupported Kubernetes schema"):
            await client.execute(
                SchemaToolRequest(
                    name=SchemaToolName.GET_K8S_RESOURCE_SCHEMA,
                    arguments={"kind": "StatefulSet", "api_version": "apps/v1"},
                )
            )

    asyncio.run(scenario())
