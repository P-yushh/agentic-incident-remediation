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
- A Hugging Face router adapter plus deterministic offline model doubles
- Environment-based configuration for Redis and Hugging Face inference
- JSON structured logging
- Redis development service

The graph currently stops at the `generating` boundary after producing a validated plan. Artifact
generation, MCP services, validation sandbox, approval API, and demonstration UI will be added
incrementally.

## Planning workflow

```text
raw alert -> normalize incident -> prepare planning -> planner
                                                   |-- valid plan -> generating boundary
                                                   `-- invalid output -> failed safely
```

Automated tests use `FakeStructuredModel`; they never call Hugging Face. A live demonstration can
construct `HuggingFaceStructuredModel.from_settings(get_settings())` after setting `HF_TOKEN`.
The live adapter requests JSON output constrained by the `RemediationPlan` schema and does not
execute or apply the proposed infrastructure changes.
