# Interview Demo Runbook

The recommended interview path is deterministic and credential-free. It demonstrates the system
design without depending on provider availability, token quotas, Docker, Redis, or GitHub writes.

## Start the demo

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
agentic-remediation-demo
```

Open <http://127.0.0.1:8000>. The API reference is available at
<http://127.0.0.1:8000/docs>, health at `/health`, and Prometheus metrics at `/metrics`.

## Suggested walkthrough

1. Submit the pre-filled OOMKilled incident.
2. Explain that the planner and generator return strict structured data rather than free-form text.
3. Point out the generated manifest, passing sandbox result, and `awaiting_approval` status.
4. Approve the proposal and show the simulated pull-request URL.
5. Submit another incident and reject it to show that no repository action occurs.
6. Emphasize that the engine proposes reviewed changes; it never applies them.

The source payload used by the demonstration is in `demo/incidents/oom-killed.json`.

## API-only walkthrough

```bash
curl -sS -X POST http://127.0.0.1:8000/api/incidents \
  -H 'content-type: application/json' \
  --data @demo/incidents/oom-killed.json
```

Use the returned `execution_id` with `GET /api/executions/{execution_id}`. Submit the returned
`approval.id` to `POST /api/executions/{execution_id}/approval` with an `approved` or `rejected`
decision and a reviewer identity.

## Live integration mode

Live mode is intentionally separate:

```bash
cp .env.example .env
docker compose up -d redis
# Build the validator images documented in the README.
# Set HF_TOKEN and GITHUB_TOKEN only for a controlled live demonstration.
agentic-remediation-api
```

Use a disposable repository or tightly scoped token. An approved live execution can create a
branch and pull request, but it cannot merge or apply the generated artifact.
