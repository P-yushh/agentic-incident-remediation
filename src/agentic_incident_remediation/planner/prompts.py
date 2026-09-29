"""Prompts for producing bounded infrastructure remediation plans."""

from agentic_incident_remediation.models import Incident

PLANNER_SYSTEM_PROMPT = """You are the planning component of a safety-first infrastructure
remediation system. Produce only a remediation plan matching the supplied JSON schema.
Never claim that a change was applied. Every plan must generate a proposed artifact, validate it
inside a sandbox, and request human approval as its final step. Prefer the smallest reversible
change and record assumptions or operational risks explicitly."""


def build_planner_prompts(incident: Incident) -> tuple[str, str]:
    """Build deterministic prompts from a normalized incident."""

    incident_json = incident.model_dump_json(indent=2)
    user_prompt = f"""Create a remediation plan for this normalized incident:

{incident_json}

The plan's incident_id must equal {incident.id}. Use contiguous step numbers beginning at 1.
Include investigation or schema lookup when useful, followed by generation, sandbox validation,
and human approval. Do not include an infrastructure apply operation."""
    return PLANNER_SYSTEM_PROMPT, user_prompt
