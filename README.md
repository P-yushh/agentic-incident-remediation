# Agentic Incident Remediation

A safety-first multi-agent workflow that turns infrastructure incidents into validated,
human-approved remediation pull requests.

The system will accept incident alerts, plan a remediation, generate Kubernetes or Terraform
artifacts, validate them in isolated containers, correct failures through a bounded reflection
loop, and pause for human approval before creating a GitHub pull request.

## Safety boundary

The portfolio implementation creates proposed changes and pull requests. It does not apply
changes to a real Kubernetes cluster or cloud account automatically.

## Development prerequisites

- Python 3.12
- Docker with Docker Compose
- A Hugging Face access token only when running live-model demonstrations

## Local setup

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
docker compose up -d redis
pytest
```

Automated tests use deterministic model doubles and do not consume hosted-model credits.

## Current foundation

- Strict Pydantic contracts for incidents, plans, generated artifacts, validation results,
  approvals, messages, and workflow state
- Immutable state-update helpers with explicit transition validation
- Alert normalization for provider-shaped Kubernetes and Terraform incidents
- A LangGraph planning workflow with explicit success and safe-failure routes
- Schema-constrained remediation planning through a provider-neutral model interface
- Kubernetes and Terraform artifact generation with incident-aware output selection
- Repository-confined paths, execution-command guards, checksums, and linked revisions
- A read-only MCP schema server with typed Kubernetes and Terraform discovery tools
- Incident-aware schema tool selection that grounds generator prompts before code generation
- A Hugging Face router adapter plus deterministic offline model doubles
- Environment-based configuration for Redis and Hugging Face inference
- JSON structured logging
- Redis development service

The graph currently stops at the `validating` boundary after producing a typed, revisioned
artifact grounded by read-only infrastructure schema context. The isolated validation sandbox,
approval API, and demonstration UI will be added incrementally.

## Planning workflow

```text
raw alert -> normalize incident -> prepare planning -> planner -> generator
                                                   |              |-- valid artifact -> validating
                                                   |              `-- unsafe output -> failed safely
                                                   `-- invalid plan -> failed safely
```

Automated tests use `FakeStructuredModel`; they never call Hugging Face. A live demonstration can
construct `HuggingFaceStructuredModel.from_settings(get_settings())` after setting `HF_TOKEN`.
The live adapter requests JSON output constrained by the `RemediationPlan` and `ArtifactDraft`
schemas and does not execute or apply the proposed infrastructure changes.

Generated files are proposals only. They must remain inside `remediations/`, use a format matching
their incident type, and pass the future sandbox validation layer before human review.

## Schema MCP server

The local MCP server exposes two read-only tools:

- `get_k8s_resource_schema(kind, api_version)`
- `get_terraform_provider_docs(provider, resource)`

Run it over the standard MCP stdio transport with:

```bash
agentic-remediation-schema-mcp
```

The checked-in catalog is intentionally limited to the resource types used by the synthetic demo.
It reads no cluster, cloud account, filesystem, or credentials. Unknown resource requests fail
closed, and returned schema context still requires the separate sandbox validation stage.
