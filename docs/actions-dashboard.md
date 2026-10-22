# Actions-backed dashboard

Run the Sand & Teal console on a Docker-prohibited laptop while GitHub performs the live work.
This is a local, single-user demonstration controller, not an authenticated public web service.
The offline console remains unchanged and uses its own entry point.

The Actions console restores the approved workspace prototype: sidebar navigation, incident
cards, visual workflow progress, **Investigation / Proposed fix / Activity** tabs, the simulator
side panel, and a structured review/publication area, all in Sand & Teal. These sections use
saved run metadata and verified evidence, not the prototype's sample metrics, agent traces,
diffs or PR numbers. The proposed fix is a single scrollable full-file code block, without display-mode
tabs, line numbers or hunk headers. It compares the exact verified proposal against a checked-in
synthetic reference baseline: red `-` lines are removed, green `+` lines are added, and unchanged
context stays neutral. All unchanged lines from the start through the end of the file are
included, not just snippets around edits. The block has a capped height and supports keyboard
scrolling. Counts appear in the file header. This baseline was not captured from
the run, a repository file, or a real cluster; provenance and approval policy disclose that limitation.
The download retains the exact verified content without diff markers. If comparison is
unavailable, the same block shows the exact file without highlights and makes no diff claim.
An unchanged proposal also shows the exact file with an explicit no-differences note.
Generate/validate progress is grouped
at GitHub job granularity, not invented
per-agent telemetry. Pause/resume buttons are absent; approval opens GitHub.

The queue shows a short `INC-` alias, title, service/platform, stage and elapsed time. Aliases
derive from local UUIDs (with collision-aware prefix extension); they do not replace persisted
identifiers or GitHub run numbers, which remain in provenance. **Proposals** is a real filtered
workspace showing only verified proposals currently awaiting human review, with a distinct empty state and
active navigation. Rejected, approved, publishing, completed, cancelled and failed runs are
excluded; resolving the selected review removes it from this queue on refresh. **Incidents**
restores all saved runs. The historical-run import form and
bottom-left account/history copy are removed; existing history and the read-only import API
remain compatible. Verdict sits below the file: WIP, Awaiting review, Rejected, Failed,
Cancelled, Approved / PR created, or Publication unverified. A receipt does not establish that
the PR was merged; post-publication PR review and merge events are not synchronized.

The sun/moon button switches between Sand & Teal and its dark counterpart, with a locally saved
appearance preference. Repetitive synthetic/demo labels are omitted from the main workspace,
but the simulator's explicit inference consent, source provenance, reference-baseline disclosure
and review/safety policy remain accurate. This presentation update does not add live monitoring,
production readiness, deployment or automatic PR merges.

Validation statistics beside the validator result show recorded tool execution attempts,
successful results and the execution's reflection/retry count. They come from the verified
manifest, not a status label or elapsed time. Legacy cached evidence is enriched only by
re-verifying an available original artifact with identical core review fields. If that artifact
has expired, counts remain explicitly unknown. These are validator executions, not GitHub job
reruns. The two-call workflow does not gain extra inference retries from this display.

Tab panels fade in, the simulator drawer transitions in/out, and only the actual active
workflow phase pulses. Completed/rejected phases do not continue animating. Reduced-motion
preferences disable animation and transitions. Connection labels/details are removed from
the main workspace; readiness and errors are presented in the simulator instead.
The simulator omits provider branding and routine ready-state/approval notices. Its availability
message appears only while checking or when starting is blocked. The simulator has no technical
scope preview: it shows the scenario, service and incident, then **Cancel / Simulate**. Removing
the preview does not remove or change the fixed backend scenario instructions. Copy accurately
describes a simulated incident, not an injected live-cluster failure. The Simulate button's tooltip
states the two-call limit, while review policy remains available separately.

### Human rejection versus technical failure

Rejecting a proposal at the `remediation-demo-review` GitHub environment gate may give the
workflow a raw `failure` conclusion. The dashboard reads the run's native review history:
an explicit rejection for that environment, a successful prepare job, and a publication job
that failed without executing any steps produce **Rejected by reviewer**, not **Workflow failed**.
A red decision card identifies the reviewer, the progress track stops at human review,
and Activity records the decision. The exact proposal stays available for inspection; no
publication, merge or deployment is claimed. The raw GitHub conclusion remains in the record.

Technical errors, cancellation, approvals followed by publisher failures, unrelated review
environments and ambiguous history are not relabelled as rejection. If review history is
unavailable, a notice explains that failure alone cannot confirm rejection. Already verified
rejection evidence survives temporary lookup failure. This reads metadata only, never starts
inference, approves a review or creates a PR. It describes the **pre-publication proposal gate**;
requests for changes or closing an already-created PR are separate events, not this status.

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

## Temporary experimental Ollama testing

Start with `agentic-remediation-actions --experimental-ollama` to enable the
**Ollama · experimental** choice in **Simulate incident → Generation mode**.
Restart without that flag to disable it again. Gemini remains the default and there is
no automatic provider fallback. The installed `qwen3:8b` failed the nine-trial reliability
gate; this opt-in mode is for testing, not a claim that the model is ready for demonstrations.

Ollama runs on the laptop's loopback interface. Choose 2,048, 4,096 or 8,192 output tokens
per call and a 5-, 10- or 20-minute timeout. Defaults are 8,192 tokens and 10 minutes,
with a 16,384-token context. These replace the benchmark's 1,200-token/90-second budgets;
they are not unlimited inference. Model context, RAM, generated file size and GitHub
input-size limits still apply. Each incident gets at most one planner call and one generator
call, with no automatic retry, repair, model download, cloud alias or Gemini fallback.

The updated, source-verified workflow must first be published to `main`. Until then the
simulator explains that the Ollama handoff is unavailable; it does not silently use Gemini
or consume local inference. On submission the server checks for the installed model without
warming it up, saves the request intent, and generates in a background worker. The queue shows
**Generating locally · Ollama** and continues polling without waiting for a GitHub run ID.
Cancellation may let the current model request finish, but prevents another node call and
prevents handing that incident to GitHub. An unresolved local run after server restart can
be cancelled; it is never automatically resumed or regenerated.

Only a bounded, typed synthetic plan/artifact is sent as workflow input, not an executable
checkpoint or shell command. GitHub replays the two typed stages without hosted inference,
validates the unchanged artifact in the existing real Docker sandbox, and saves the original
Redis review checkpoint. The manifest distinguishes local generation and zero remote inference
calls. PR publication still requires the same explicit human-review environment; no merge or
deployment is added. Local generation alone is never displayed as a validated proposal.
Only these fixed synthetic scenarios are supported; inputs become part of a public GitHub run.

Local errors and GitHub job failures remain distinct. Ambiguous dispatches keep the existing
no-retry lock, and changing the provider, scenario or inference budget requires a new request
identity. The dashboard does not adjust billing or verify free-tier eligibility.

## Inspect saved runs without inference

Saved incidents load automatically from the local journal, including previously imported runs.
Select a queue card, or choose **Proposals** to inspect proposals awaiting human review.
There is no historical-run import form in the interface. Viewing saved runs does not start a
workflow, call a model, cancel a workflow, approve a review or modify GitHub.
Imported runs cannot be cancelled from this dashboard. Remote artifacts expire after one day;
if evidence is unavailable, the UI must not claim validation passed or invent a PR receipt.
Verified evidence already downloaded into local history remains available after remote expiry.

## Start a new live demonstration

1. Open **Simulate incident** and check the readiness message. Readiness verifies the configured public
   repository, active workflow, approved workflow-source digest, required human reviewers and
   repository PR-creation policy. It does **not** check provider billing/quota or prove that the
   credential has dispatch permission.
2. Click **Simulate incident** and select one of the three allowlisted synthetic scenarios:
   payment-api OOM, queue-worker CrashLoop/configuration reference, or checkout Terraform ingress
   drift. Service/title/details update from the shared scenario catalog and remain read-only.
   Each run uses only its selected case, not all three.
   Previewing the selector does not call Gemini. New live dispatch stays disabled until the
   matching multi-scenario workflow is published to main; historical OOM evidence remains readable.
3. Click **Simulate** only when ready (up to two AI calls, indicated by its tooltip). This deliberate button press
   authorizes that one bounded run; there is no separate checkbox. No model request is sent by
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
the exact GitHub job where available. Queue failures and human rejection are red. Verdict identifies
failure during GitHub execution rather than presenting approval instructions as its outcome.
The numbered failed phase is red; earlier phases are green only when confirmed by job/evidence
metadata, and later phases are marked not run. Unknown phase/approval progress is not invented.

The preparation adapter records an allowlisted provider failure category before the workflow
converts exceptions into failed state. The runner emits only `AIR_FAILURE_V1` JSON in a GitHub
check annotation; bodies, prompts, credentials and raw exception text are never printed. The
dashboard reads that marker only from the same repository's failed-job check-run URL, rejects
extra fields/unknown or conflicting codes, and maps it to a fixed public message. Raw job logs
are not downloaded by the dashboard, and arbitrary annotation text is never displayed. A verified
HTTP 429 category says **quota or rate limit reached**, not that a daily quota was exhausted.
Authentication, provider server/network errors and invalid model output are separate categories.

Older runs emitted only a generic failure message. They remain **cause unavailable; quota not
confirmed**: the missing historical reason cannot be reconstructed by this update. New runner
diagnostics require publishing this code before they can be emitted on GitHub. Viewing or
classifying failures does not run inference or retry the failed workflow. Local Ollama fallback
is not implemented by this failure-display update.

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
