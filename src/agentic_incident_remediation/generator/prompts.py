"""Prompts for producing proposed Kubernetes and Terraform artifacts."""

from agentic_incident_remediation.models import AgentState, ArtifactKind
from agentic_incident_remediation.schema_tools.models import SchemaDocument

GENERATOR_SYSTEM_PROMPT = """You are the code-generation component of a safety-first
infrastructure remediation system. Return exactly one artifact matching the supplied JSON schema.
Generate declarative Kubernetes YAML or Terraform HCL only. Never include shell commands,
kubectl apply, terraform apply, terraform destroy, credentials, or secret values. Write only under
the remediations directory and make the smallest reversible change described by the plan."""


def build_generator_prompts(
    state: AgentState,
    expected_kind: ArtifactKind,
    schema_context: SchemaDocument | None = None,
) -> tuple[str, str]:
    """Build a deterministic generation prompt including correction context when present."""

    if state.plan is None:
        raise ValueError("generator prompt requires a remediation plan")

    extension = ".yaml" if expected_kind is ArtifactKind.KUBERNETES_MANIFEST else ".tf"
    sections = [
        "Generate one proposed infrastructure artifact for this incident and plan.",
        f"Expected kind: {expected_kind.value}",
        f"Required path: remediations/<descriptive-name>{extension}",
        f"Incident:\n{state.incident.model_dump_json(indent=2)}",
        f"Plan:\n{state.plan.model_dump_json(indent=2)}",
    ]
    if schema_context is not None:
        sections.append(
            "Read-only infrastructure schema context returned by the selected MCP tool:\n"
            f"{schema_context.model_dump_json(indent=2)}"
        )
    if state.artifacts:
        previous = state.artifacts[-1]
        sections.append(
            "Previous artifact to revise without changing its path:\n"
            f"Path: {previous.path}\nContent:\n{previous.content}"
        )
    if state.validation_errors:
        sections.append("Validation errors to correct:\n- " + "\n- ".join(state.validation_errors))
    sections.append("Do not apply or execute the artifact.")
    return GENERATOR_SYSTEM_PROMPT, "\n\n".join(sections)
