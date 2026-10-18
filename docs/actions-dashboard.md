# Actions-backed dashboard

Run the Sand & Teal console on a Docker-prohibited laptop while GitHub performs the live work.
This is a local, single-user demonstration controller, not an authenticated public web service.
The offline console remains unchanged and uses its own entry point.

The Actions console restores the approved workspace prototype: sidebar navigation, incident
cards, visual workflow progress, **Investigation / Proposed fix / Activity** tabs, the simulator
side panel, and a structured review/publication area, all in Sand & Teal. These sections use
saved run metadata and verified evidence, not the prototype's sample metrics, agent traces,
diffs or PR numbers. The proposal is the full generated file because no baseline file is
available. Generate/validate progress is grouped at GitHub job granularity, not invented
per-agent telemetry. Pause/resume buttons are absent; approval opens GitHub.

## Start the console

From the repository root with the project environment active:

```bash
python -m pip install -e '.[dev]'
agentic-remediation-actions
```

Open <http://127.0.0.1:8001>. No Docker, local Redis or Gemini key is required by this process.
It uses `GITHUB_TOKEN` from the **server process environment**, or reuses the existing GitHub
Git credential helper with interactive prompting disabled. It does not create or save a token,
load the model credentials in `.env`, or send a GitHub credential to browser storage. Do not
paste credentials into the dashboard, URLs, screenshots, source files or shared terminals.

If the helper has no credential, configure an approved server-side credential yourself. It needs
repository access, Actions read/write for dispatch and cancellation, contents read for source
verification, and access to the repository workflow-permission readiness endpoint. Do not broaden
organization permissions as a workaround. Model inference still uses only the repository's
existing `GEMINI_API_KEY` Actions secret on the remote runner.

Optional local configuration:

```bash
agentic-remediation-actions --port 8001 \
  --repository P-yushh/agentic-incident-remediation \
  --history .agent-runs/actions-dashboard.sqlite3
```

The server always binds to `127.0.0.1`. The repository cannot be changed through a browser request.
The SQLite journal is Git-ignored, owner-readable/writable only, and contains metadata and
synthetic review evidence, not credentials. Keep it when restarting the app: it is also the
durable protection against repeating an uncertain dispatch.

## Inspect the existing successful demonstration without inference

Enter **39124344483** under **Existing GitHub run ID (read-only)** and click **Load existing run**.
This imports the verified historical run behind [PR #11](https://github.com/P-yushh/agentic-incident-remediation/pull/11).
It does not start a workflow, call a model, cancel a workflow, approve a review or modify GitHub.
Imported runs cannot be cancelled from this dashboard. Remote artifacts expire after one day;
if evidence is unavailable, the UI must not claim validation passed or invent a PR receipt.
Verified evidence already downloaded into local history remains available after remote expiry.

## Start a new live demonstration

1. Check the **ACTIONS** banner and readiness message. Readiness verifies the configured public
   repository, active workflow, approved workflow-source digest, required human reviewers and
   repository PR-creation policy. It does **not** check provider billing/quota or prove that the
   credential has dispatch permission.
2. Click **Simulate incident** and select one of the three allowlisted synthetic scenarios:
   payment-api OOM, queue-worker CrashLoop/configuration reference, or checkout Terraform ingress
   drift. Service/title/details update from the shared scenario catalog and remain read-only.
   Each run uses only its selected case, not all three. Changing the scenario resets consent.
   Previewing the selector does not call Gemini. New live dispatch stays disabled until the
   matching multi-scenario workflow is published to main; historical OOM evidence remains readable.
3. Deliberately check the consent box for **at most two Gemini calls**, then click
   **Start confirmed live run**. The checkbox resets every time. No model request is sent by
   opening the dashboard, checking readiness, importing history or polling progress.
4. Follow the selected run. Progress is polled at five-second intervals with error backoff,
   not streamed from a Kubernetes cluster. The current remote job step is shown when available.
5. Once preparation succeeds, inspect the proposal, real validator evidence, checksum and
   execution ID. The displayed artifact must pass provenance, archive-digest, content-checksum,
   validation and approval-binding checks before the console labels it verified.
6. Click **Review in GitHub**. In GitHub, review the proposal and use
   **Review deployments → remediation-demo-review → Approve and deploy** only if satisfied.
   The button authorizes **PR publication**, not infrastructure deployment. Rejecting/cancelling
   in GitHub prevents publication. The local server has no approval, bypass or merge endpoint.
7. Return to the dashboard. After publication, it shows the verified receipt and real PR link,
   and offers proposal/receipt downloads. It correlates execution, approval and artifact checksum
   between the saved review and publication receipt. A successful Actions label alone is not
   treated as proof that a receipt was retrieved or a proposal validated.

Keep the Gemini project on billing-unset configuration and check its current free allowance
before consenting. The application does not configure billing or guarantee free-tier availability.
Do not merge the resulting synthetic remediation PR just to complete the demonstration.

## Controls and timers

- **No pause/resume:** the two-job Actions workflow has no cooperative per-node pause/resume API.
  Those controls remain available only in the existing native offline/live console.
- **Cancel workflow:** offered only for active runs started through this local journal. It requests
  cancellation after an explicit warning. It cannot undo an existing PR or guarantee interruption
  of a write already in flight. The UI waits for GitHub's status before reporting cancellation.
- **Elapsed time:** before a review bundle exists, the console labels time as run elapsed, not
  alert age. Once verified evidence arrives, it shows alert age frozen at the native approval's
  request timestamp. A timer is not token, model latency or incident-recovery telemetry.
- **Reload:** server-side run history survives browser refresh and server restart. The native
  in-memory fixture console still has its original reset behavior.

## Scenario and failure evidence

Both Kubernetes cases use real kubeconform; Terraform uses the existing isolated Terraform
validator and bundled provider. The runner builds only the selected validator before receiving
the model secret. Each case shares the same hard two-invocation budget and required-review gate.
CrashLoop assumes a synthetic ConfigMap exists; schema validation does not establish actual
workload recovery. Terraform IDs/CIDRs are synthetic inputs, not a connection to AWS.

Version-2 review manifests bind the selected scenario to the native incident. The dashboard checks
artifact family, validator, exit code, content checksum, approval and scenario-specific PR branch.
Version-1 historical manifests remain OOM-only. The internal bundle retains `proposal.yaml` for
handoff compatibility even when its contents are Terraform; dashboard downloads use `proposal.tf`.

Failed runs display a known failed job/step label, a conservative failure category, and a link to
the exact GitHub job where available. Raw job logs and arbitrary annotation messages are not
downloaded or shown by the controller. A generation failure cannot reliably be called a quota
error without inspecting logs; the UI therefore lists possible checks without claiming a cause.

Run `python -m pytest tests/actions` for API and controller regressions. The controller checks run
the actual JavaScript in Node's isolated deterministic DOM harness (no npm packages or network).
Node 24 is configured in quality CI; locally the controller test skips if Node is unavailable.
These are controller/contract tests, not automated Chromium rendering or a live GitHub run.
Real-browser smoke checks are separate, as are the opt-in Docker/Redis integration tests covering
all three scenarios' original-checkpoint handoff and simulated publication.

## Duplicate and ambiguous requests

Each explicit start has a UUID. The server journals that intent **before** sending its only
dispatch request and serializes new intents across SQLite connections. Repeated requests with
that UUID return the existing record, including after restart. Only one active or uncertain
dashboard-started intent is allowed in this repository's journal at a time.

The client requests GitHub's documented `return_run_details` response; it never guesses a run by
choosing the newest run in the repository. If the request times out, GitHub returns no run ID,
or a server failure leaves acceptance uncertain, the UI records **dispatch outcome uncertain**.
There is no automatic HTTP retry or workflow rerun. Inspect GitHub before any recovery. Do not
delete the journal, generate another request ID, or use **Re-run jobs** to bypass that protection.
Importing a run is read-only and does not clear an uncertain intent or prove correlation with it.

The browser retains the pending request UUID (not credentials) if its own connection fails, so
a deliberate resubmission uses the same idempotency key. This guard does not cover workflow
dispatches made outside this dashboard or deletion/copying of its journal.

## Local security and artifact boundaries

Host and client-address checks reject non-loopback access and unexpected hosts. Mutating routes
require an exact same-origin header, a per-process session nonce and JSON content type. The
server grants no cross-origin access, disables framing and caching, and provides no public/tunnel
configuration. Do not expose this credential-bearing local controller through a reverse proxy,
port forward, shared host or public URL. Other processes running as the same local user remain
inside the trust boundary; this is not multi-user authentication.

Artifact archives are bounded in compressed and uncompressed size, matched to the exact run,
and checked against GitHub's SHA-256 digest. Downloads follow only approved HTTPS storage hosts
with **no GitHub Authorization header**. Unknown/duplicate/path-traversal ZIP entries are rejected.
The Redis RDB is never deserialized, executed, extracted to disk or returned to the browser.
Only verified synthetic proposal/receipt projections are retained locally.

The workflow digest is intentionally pinned to the previously reviewed two-call workflow. Changing
that workflow disables new dashboard dispatches until its new budget and trust boundaries are
reviewed and the pin/tests deliberately updated. Imported runs also verify the workflow source
at their own recorded commit. This pin does not substitute for reviewing Python changes on main.

## Verification scope

Automated tests use real local FastAPI/SQLite boundaries and mocked GitHub HTTP. They cover
consent, nonce/Host/Origin checks, one-shot dispatch, restart idempotency, concurrency, uncertain
outcomes, progress, read-only import, cancellation, expired/tampered artifacts and receipt binding.
They never call Gemini or mutate a real repository. A read-only import of a historical real run
checks retrieval and rendering, not fresh dashboard-initiated inference. A new full hosted run
requires a separate, deliberate two-call consent and GitHub human approval.

Primary API references: [workflow dispatch and returned run details](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[run status and cancellation](https://docs.github.com/en/rest/actions/workflow-runs),
[artifact downloads and digests](https://docs.github.com/en/rest/actions/artifacts).
