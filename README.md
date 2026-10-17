# Agentic Incident Remediation

A safety-first multi-agent workflow that turns infrastructure incidents into validated,
human-approved remediation pull requests.

The system accepts incident alerts, plans a remediation, generates Kubernetes or Terraform
artifacts, validates them in isolated containers, corrects failures through a bounded reflection
loop, and pauses for human approval before creating a GitHub pull request.

## Safety boundary

The implementation creates proposed changes and pull requests. It does not apply
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
- Redis-backed LangGraph checkpoints keyed by execution ID, with state restoration and history
- A durable native interrupt that pauses until a matching human approval or rejection arrives
- An idempotent GitHub publisher restricted to branches, one generated artifact, and pull requests
- A Hugging Face router adapter plus deterministic offline model doubles
- Environment-based configuration for Redis, Hugging Face inference, and GitHub operations
- JSON structured logging
- A FastAPI control plane with execution lookup, approval endpoints, and server-sent state events
- A responsive review console plus health and Prometheus metrics endpoints
- A credential-free deterministic demo covering Kubernetes and Terraform scenarios
- Pull-request CI for linting, formatting, strict typing, tests, and a 90% coverage gate
- Redis development service

The full graph retries invalid artifacts through a bounded reflection loop and checkpoints every
node boundary. A valid artifact creates a stable approval request and durably interrupts execution.
Approval resumes the same execution and can create a pull request; rejection exits without a
repository write. Exhausted retries, sandbox errors, and repository failures terminate safely.
The browser console and API expose this lifecycle without weakening its approval boundary.

## Credential-free demo

Run the complete system locally without Hugging Face, Redis, Docker, GitHub credentials, or any
billing risk:

```bash
agentic-remediation-demo
```

Then open <http://127.0.0.1:8000>. This mode uses deterministic adapters but exercises the real
FastAPI routes, LangGraph state machine, validation routing, approval interrupt, and pull-request
capability boundary. The returned pull-request URL is simulated and no network call is made.

See [the architecture guide](docs/architecture.md) for component and trust boundaries.

### Dashboard and execution controls

The dashboard uses a light Sand & Teal palette. **Simulate incident** opens a side panel for
creating synthetic alerts. Submitted incidents appear in a browser-session queue with their
generated proposal, validation status, and human approval controls. Refreshing the page clears
this local queue; persisted executions remain accessible by ID when using Redis.

Each incident displays elapsed wall-clock time since its alert timestamp, including paused time.
The timer freezes at the approval request, or at failure or termination before review.

- **Pause** stops scheduling new workflow nodes after the current node finishes.
- **Resume** continues the same checkpointed execution without bypassing human review.
- **Terminate** stops further workflow work and prevents subsequent approval or PR publication.
  It does not undo an already published pull request.

In-flight external calls must finish before a requested pause or termination takes effect.
Pause is disabled at human review, and finished executions or repository publication already
in progress cannot be controlled. Background processing uses four workers in one API process;
pending controls are process-local until the current node finishes and checkpoints them.

The dashboard submits incidents with `POST /api/incidents?background=true`. Omitting the query
parameter retains synchronous processing. The deterministic runtime adds a short delay between
background steps so state changes and controls are visible; live processing adds no such delay.

### Live integrations

Live mode uses Hugging Face inference, Redis checkpoints, Docker validators, and the GitHub API.
Build the validator images described below, configure `HF_TOKEN` and `GITHUB_TOKEN` in a local
environment, and run:

```bash
docker compose up -d redis
agentic-remediation-api
```

Use a disposable repository and a tightly scoped token for integration testing. Human approval
may create a real branch and pull request, but it cannot merge or apply the generated changes.

## HTTP API

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/incidents` | Normalize an incident and run until approval or safe failure |
| `POST` | `/api/incidents?background=true` | Start background processing and return the persisted incident |
| `GET` | `/api/executions/{id}` | Restore the latest persisted execution state |
| `GET` | `/api/executions/{id}/events` | Stream state changes as server-sent events |
| `POST` | `/api/executions/{id}/control` | Request `pause`, `resume`, or `terminate` using the `action` field |
| `POST` | `/api/executions/{id}/approval` | Resume with a correlated approve/reject decision |
| `GET` | `/health` | Report process liveness without exposing configuration |
| `GET` | `/metrics` | Export low-cardinality Prometheus counters |

## Planning workflow

```text
raw alert -> normalize incident -> prepare planning -> planner -> generator
                                                   |              |-- valid artifact -> validator
                                                   |              `-- unsafe output -> failed safely
                                                   `-- invalid plan -> failed safely

validator -> passed -> persisted human approval interrupt
          |-- invalid artifact -> reflector -> retry available -> generator
          |                              `-- retry ceiling -> failed safely
          `-- sandbox error -> failed safely

approval -> rejected -> stop without repository changes
         `-- approved -> publish validated artifact branch -> create pull request -> completed
```

Automated tests use `FakeStructuredModel`; they never call Hugging Face. A live demonstration can
construct `HuggingFaceStructuredModel.from_settings(get_settings())` after setting `HF_TOKEN`.
The live adapter requests JSON output constrained by the `RemediationPlan` and `ArtifactDraft`
schemas and does not execute or apply the proposed infrastructure changes.

Generated files are proposals only. They must remain inside `remediations/`, use a format matching
their incident type, and pass sandbox validation before human review. Reflection itself is
deterministic and makes no model request; only the generator requests a corrected artifact using
the previous revision and the validator's structured feedback.

## Persistence and human approval

`create_redis_checkpointer()` initializes the indices used by LangGraph before a workflow is
compiled. The development service uses Redis 8, which includes the JSON and search capabilities
required by the Redis checkpointer. An execution UUID is also its checkpoint thread ID, allowing a
new process to restore the latest state or inspect prior snapshots.

`DurableApprovalWorkflow.start()` runs until LangGraph's native `interrupt()` pauses at human
review. `resume()` requires the pending approval UUID, a final approve/reject decision, and reviewer
identity. A mismatched or stale approval UUID is rejected before execution resumes.

For offline demonstrations and tests, `InMemorySaver` and `FakePullRequestPublisher` provide the
same workflow behavior without Redis, GitHub, or hosted-model calls.

## Approved repository operations

The live `GitHubPullRequestPublisher` requires `GITHUB_TOKEN` and performs only these operations:

1. Reuse an existing pull request for the execution branch when present.
2. Create a deterministic remediation branch from the configured base branch.
3. Commit the validated artifact under `remediations/`.
4. Open a pull request containing the validation and approval audit context.

The publisher refuses to overwrite conflicting branch content. It has no Kubernetes, Terraform,
shell, deployment, merge, or production-apply capability.

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
