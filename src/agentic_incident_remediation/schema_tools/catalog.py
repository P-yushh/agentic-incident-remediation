"""Deterministic infrastructure schema snapshots used by MCP tools."""

import re

from agentic_incident_remediation.schema_tools.errors import SchemaLookupError
from agentic_incident_remediation.schema_tools.models import (
    AttributeRequirement,
    KubernetesResourceSchema,
    SchemaField,
    TerraformProviderDocs,
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,149}$")

_DEPLOYMENT_SCHEMA = KubernetesResourceSchema(
    kind="Deployment",
    api_version="apps/v1",
    schema_version="kubernetes-openapi-v1.31-curated",
    fields=(
        SchemaField(
            path="apiVersion",
            value_type="string",
            requirement=AttributeRequirement.REQUIRED,
            description="API group and version for a Deployment.",
            allowed_values=("apps/v1",),
        ),
        SchemaField(
            path="kind",
            value_type="string",
            requirement=AttributeRequirement.REQUIRED,
            description="Kubernetes resource kind.",
            allowed_values=("Deployment",),
        ),
        SchemaField(
            path="metadata.name",
            value_type="DNS subdomain string",
            requirement=AttributeRequirement.REQUIRED,
            description="Stable name of the workload being remediated.",
        ),
        SchemaField(
            path="spec.selector.matchLabels",
            value_type="map[string]string",
            requirement=AttributeRequirement.REQUIRED,
            description="Labels used by the Deployment to select its Pods.",
        ),
        SchemaField(
            path="spec.template.metadata.labels",
            value_type="map[string]string",
            requirement=AttributeRequirement.REQUIRED,
            description="Pod labels; they must satisfy the Deployment selector.",
        ),
        SchemaField(
            path="spec.template.spec.containers",
            value_type="array[Container]",
            requirement=AttributeRequirement.REQUIRED,
            description="At least one container specification for the Pod template.",
        ),
        SchemaField(
            path="spec.template.spec.containers[].name",
            value_type="DNS label string",
            requirement=AttributeRequirement.REQUIRED,
            description="Container name used when targeting a resource-limit correction.",
        ),
        SchemaField(
            path="spec.template.spec.containers[].resources.requests.memory",
            value_type="Kubernetes quantity string",
            requirement=AttributeRequirement.OPTIONAL,
            description="Memory reserved for scheduling; keep it no greater than the limit.",
        ),
        SchemaField(
            path="spec.template.spec.containers[].resources.limits.memory",
            value_type="Kubernetes quantity string",
            requirement=AttributeRequirement.OPTIONAL,
            description="Maximum container memory before an OOM termination can occur.",
        ),
    ),
    safety_notes=(
        "Preserve selectors and existing labels when changing container resources.",
        "Return declarative YAML only; never include kubectl execution commands.",
        "A schema lookup does not prove that a manifest is safe or deployable.",
    ),
    source_url="https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/",
)

_SECURITY_GROUP_RULE_DOCS = TerraformProviderDocs(
    provider="hashicorp/aws",
    resource="aws_security_group_rule",
    provider_version="5.x-curated",
    fields=(
        SchemaField(
            path="type",
            value_type="string",
            requirement=AttributeRequirement.REQUIRED,
            description="Direction of the rule.",
            allowed_values=("ingress", "egress"),
        ),
        SchemaField(
            path="from_port",
            value_type="number",
            requirement=AttributeRequirement.REQUIRED,
            description="Start of the permitted port range.",
        ),
        SchemaField(
            path="to_port",
            value_type="number",
            requirement=AttributeRequirement.REQUIRED,
            description="End of the permitted port range.",
        ),
        SchemaField(
            path="protocol",
            value_type="string",
            requirement=AttributeRequirement.REQUIRED,
            description="IP protocol such as tcp, udp, icmp, or -1 for all protocols.",
        ),
        SchemaField(
            path="security_group_id",
            value_type="string",
            requirement=AttributeRequirement.REQUIRED,
            description="Identifier of the security group that owns the rule.",
        ),
        SchemaField(
            path="cidr_blocks",
            value_type="set[string]",
            requirement=AttributeRequirement.OPTIONAL,
            description="IPv4 CIDR sources or destinations; avoid unrestricted ranges.",
            conflicts_with=("source_security_group_id",),
        ),
        SchemaField(
            path="ipv6_cidr_blocks",
            value_type="set[string]",
            requirement=AttributeRequirement.OPTIONAL,
            description="IPv6 CIDR sources or destinations; avoid unrestricted ranges.",
            conflicts_with=("source_security_group_id",),
        ),
        SchemaField(
            path="source_security_group_id",
            value_type="string",
            requirement=AttributeRequirement.OPTIONAL,
            description="Security group used as the traffic source for an ingress rule.",
            conflicts_with=("cidr_blocks", "ipv6_cidr_blocks"),
        ),
        SchemaField(
            path="description",
            value_type="string",
            requirement=AttributeRequirement.OPTIONAL,
            description="Human-readable purpose of the rule.",
        ),
    ),
    safety_notes=(
        "Prefer narrow CIDR ranges; do not introduce 0.0.0.0/0 or ::/0 without explicit review.",
        "Return declarative HCL only; never include terraform apply or destroy commands.",
        "Provider documentation does not replace terraform validate or policy checks.",
    ),
    source_url=(
        "https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/"
        "security_group_rule"
    ),
)

_KUBERNETES_SCHEMAS = {
    (_DEPLOYMENT_SCHEMA.kind.casefold(), _DEPLOYMENT_SCHEMA.api_version.casefold()): (
        _DEPLOYMENT_SCHEMA
    )
}
_TERRAFORM_DOCS = {
    (
        _SECURITY_GROUP_RULE_DOCS.provider.casefold(),
        _SECURITY_GROUP_RULE_DOCS.resource.casefold(),
    ): _SECURITY_GROUP_RULE_DOCS
}


def get_k8s_resource_schema(kind: str, api_version: str) -> KubernetesResourceSchema:
    """Return the approved Kubernetes schema subset for a resource."""

    safe_kind = _validate_identifier(kind, "kind")
    safe_api_version = _validate_identifier(api_version, "api_version")
    schema = _KUBERNETES_SCHEMAS.get((safe_kind.casefold(), safe_api_version.casefold()))
    if schema is None:
        raise SchemaLookupError(
            f"unsupported Kubernetes schema: {safe_api_version}/{safe_kind}; "
            "supported: apps/v1/Deployment"
        )
    return schema


def get_terraform_provider_docs(provider: str, resource: str) -> TerraformProviderDocs:
    """Return approved Terraform provider fields for a resource."""

    safe_provider = _validate_identifier(provider, "provider")
    safe_resource = _validate_identifier(resource, "resource")
    docs = _TERRAFORM_DOCS.get((safe_provider.casefold(), safe_resource.casefold()))
    if docs is None:
        raise SchemaLookupError(
            f"unsupported Terraform resource: {safe_provider}/{safe_resource}; "
            "supported: hashicorp/aws/aws_security_group_rule"
        )
    return docs


def _validate_identifier(value: str, field: str) -> str:
    normalized = value.strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        raise SchemaLookupError(f"{field} contains unsupported characters or length")
    return normalized
