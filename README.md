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
- Locked-down Docker validation for Kubernetes and Terraform artifacts
- Structured validator findings with explicit pass, reflection, and runtime-failure routes
- Deterministic reflection that turns validation findings into bounded, linked artifact revisions
- A configurable retry ceiling that fails safely instead of looping indefinitely
- A Hugging Face router adapter plus deterministic offline model doubles
- Environment-based configuration for Redis and Hugging Face inference
- JSON structured logging
- Redis development service

The full graph now retries invalid artifacts through a bounded reflection loop. Valid artifacts reach
the future approval boundary, exhausted retries and sandbox infrastructure errors fail safely, and
every correction remains linked to the artifact revision that preceded it. The approval API and
demonstration UI will be added incrementally.

## Planning workflow

```text
raw alert -> normalize incident -> prepare planning -> planner -> generator
                                                   |              |-- valid artifact -> validator
                                                   |              `-- unsafe output -> failed safely
                                                   `-- invalid plan -> failed safely

validator -> passed -> awaiting approval boundary
          |-- invalid artifact -> reflector -> retry available -> generator
          |                              `-- retry ceiling -> failed safely
          `-- sandbox error -> failed safely
```

Automated tests use `FakeStructuredModel`; they never call Hugging Face. A live demonstration can
construct `HuggingFaceStructuredModel.from_settings(get_settings())` after setting `HF_TOKEN`.
The live adapter requests JSON output constrained by the `RemediationPlan` and `ArtifactDraft`
schemas and does not execute or apply the proposed infrastructure changes.

Generated files are proposals only. They must remain inside `remediations/`, use a format matching
their incident type, and pass sandbox validation before human review. Reflection itself is
deterministic and makes no model request; only the generator requests a corrected artifact using
the previous revision and the validator's structured feedback.

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

## Isolated validation

Build the pinned local validator images before using the live Docker runner:

```bash
docker build -t agentic-remediation/kubeconform-validator:0.1.0 \
  sandbox/kubeconform-validator
docker build -t agentic-remediation/terraform-validator:0.1.0 \
  sandbox/terraform-validator
```

The runner sends artifact content through standard input and never mounts the repository, Docker
socket, credentials, or home directory. Runtime containers have networking disabled, a read-only
root filesystem, an ephemeral 16 MiB `/tmp`, dropped capabilities, `no-new-privileges`, an
unprivileged user, and CPU, memory, process, and timeout limits. Kubernetes validation uses
Kubeconform with a bundled schema. Terraform validation uses a bundled AWS provider and runs
`terraform init -backend=false` followed by `terraform validate -json`; it never plans or applies.

Automated tests use `FakeSandboxRunner`, so CI does not require Docker or download validator images.
