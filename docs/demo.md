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

The dashboard uses the light **Sand & Teal** palette: warm ivory surfaces, deep teal actions
and selection highlights, and red destructive controls. The simulator drawer shares the same
palette. Text contrast checks cover the primary, secondary, selected, and destructive colors.

## Suggested walkthrough

1. Click **Simulate incident** to open the side panel. Review the pre-filled OOMKilled alert,
   service, title, and description, then click **Create incident**.
2. Explain that the planner and generator return strict structured data rather than free-form text.
3. The panel closes and the incident appears in the session queue. Point out the generated
   manifest, passing sandbox result, and `awaiting_approval` status in the main workspace.
4. Approve the proposal and show the simulated pull-request URL.
5. Submit another incident and reject it to show that no repository action occurs.
6. Emphasize that the engine proposes reviewed changes; it never applies them.

The panel can be dismissed with Cancel, the close button, Escape, or a click outside it. Failed
submissions keep the panel and entered values available for correction or retry. The queue keeps
the executions created during this page session; refreshing the page clears this local list.

## Incident timers and execution controls

Each queue entry displays the wall-clock time since its alert timestamp. Paused time is included.
The timer freezes at the approval request timestamp and remains frozen after approval or rejection.
Failure or termination before review also stops the timer. Reviewing a different incident does not
reset any timer.

- **Pause** stops scheduling new workflow nodes after the current node finishes. A call already
  in progress displays `pause_requested` until it reaches that boundary.
- **Resume** continues the same checkpointed execution. It does not generate a second incident or
  bypass human review.
- **Terminate** permanently stops that execution at the next safe boundary and prevents approval
  or PR publication. An in-flight call must finish first. This does not undo previously published PRs.

Pause is disabled once review is ready; the existing human approval gate already holds the workflow.
Termination remains available during review. Finished incidents and repository publication already
in progress cannot be paused, resumed, or terminated.

The dashboard uses `POST /api/incidents?background=true` and
`POST /api/executions/{id}/control` with `{"action":"pause"}`, `resume`, or `terminate`.
Omitting `background=true` preserves the existing synchronous API behavior. Background processing is
limited to four workers in this local API process. The offline demo introduces a short delay between
steps so timers and controls are visible; no such delay is added to live processing.

Applied pause and termination states are checkpointed. Requested controls while a node is running
are process-local until the node finishes. A paused checkpoint can be resumed by execution ID after
a server restart; this does not restore the browser's session-only queue.

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
