# Reproducible demonstration

This is an operational runbook, not a production deployment guide. All sample alerts,
workload images, namespaces and resource IDs are synthetic. Nothing applies infrastructure,
merges a remediation PR, or configures billing.

## Choose the mode

| Mode | Model | Validation | Persistence | Publication |
| --- | --- | --- | --- | --- |
| Local offline console | Deterministic fixtures | Simulated results | In-memory | Simulated PR |
| Reviewed Actions demo | At most two Gemini invocations | Real Docker kubeconform | Original Redis checkpoints carried in an RDB snapshot | Real PR, only after human review |
| Local live console | Configured Gemini or Hugging Face adapter | Real Docker validators | Redis | Real PR, only after approval in the console |

The dashboard displays its configured mode and capability labels. These labels describe wiring,
not dependency health. `/health` is liveness only. Do not expose the unauthenticated console to
the public internet. On a laptop where Docker is prohibited, use the offline console and the
Actions demo; do not install a substitute runtime to bypass organizational restrictions.

## Local offline console

From the repository root, with Python 3.12 installed:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
agentic-remediation-check --mode offline
agentic-remediation-demo
```

Open <http://127.0.0.1:8000>, confirm the **OFFLINE** banner, select **Simulate incident**,
and create a Kubernetes alert. Inspect its proposal and simulated validation result, then
approve or reject it. An approved offline run displays a simulated PR URL; it does not prove
GitHub connectivity. Try a Terraform incident and the pause/resume/terminate controls as well.
Restarting the server resets in-memory executions. Refreshing the browser clears its queue.

Offline mode does not load `.env`, contact a hosted model, or require Docker, Redis or tokens.

## One-time setup for the reviewed Actions demo

Repository-owner changes below are manual and require explicit owner authorization. No workflow
creates an environment, creates tokens, changes permission settings, or enables billing.

1. Use this public repository with standard GitHub-hosted Ubuntu runners. Review the account's
   current Actions/artifact allowances and spending controls. Do not enable paid runner sizes,
   paid storage or provider billing. The workflow refuses private repositories.
2. Keep the Gemini project on its no-billing configuration and check its current free quota.
   Configure `GEMINI_API_KEY` as a repository Actions secret. Do not paste it in source or logs.
3. In **Settings → Environments**, create **remediation-demo-review** and add at least one
   **Required reviewer**. For a single-owner demonstration you may select yourself and leave
   **Prevent self-review** off; approval must still be a separate deliberate review action.
   With a second reviewer available, prevent self-review. Restrict deployment branches to `main`.
4. In **Settings → Actions → General → Workflow permissions**, allow GitHub Actions to create
   pull requests if repository policy permits it. The publish job requests only the required
   contents/PR writes and read access for review checks. It uses the short-lived job token;
   no new personal token or repository `GITHUB_TOKEN` secret is necessary. If policy blocks this,
   stop; do not weaken organization policy or add a broader token as a workaround.
5. Ensure these changes are reviewed, CI is green, and the workflow exists on `main`.

GitHub documents [required reviewers for public repositories](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
and the [review-history API](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run).
The preflight fails before inference if the environment is missing or lacks required reviewers.
It does not validate Gemini billing eligibility, quota, or GitHub PR-creation policy.

## Run one fresh incident through a real approved PR

```text
Manual dispatch on main (explicit maximum-two-call consent)
  → required-reviewer setup check
  → fresh synthetic OOM alert through the actual incident API
  → Gemini planner → Gemini generator (shared hard budget: 2)
  → real isolated kubeconform validation
  → native pending approval interrupt + Redis checkpoint history
  → downloadable review bundle; prepare runner exits
  → YOU inspect proposal + validation evidence and approve the environment
  → second runner verifies approval history + same-run bundle digest
  → original Redis RDB restored, exact execution/approval/artifact checked
  → actual approval API resumes the original native interrupt
  → real GitHub branch + artifact commit + open PR
  → STOP: no merge, no deployment
```

1. Open **Actions → reviewed-live-demo → Run workflow**, select `main`, and explicitly confirm
   the two-call demo. This is not permission for an automatic rerun. Normal CI never calls Gemini.
2. Wait for **prepare** to complete. Its summary contains the execution ID, native approval ID,
   artifact checksum, and bundle checksum. Download the run's **review-bundle-…** artifact.
3. Inspect `proposal.yaml` and `manifest.json`: check the intended memory change, file path,
   validation result, pending decision and synthetic workload context. `dump.rdb` carries the
   original checkpoint data and interrupt, rather than a reconstructed artifact-only state.
4. Only if satisfied, use **Review deployments → remediation-demo-review → Approve and deploy**.
   GitHub's button says “deploy,” but here it authorizes **PR publication only**. The script
   reads GitHub's actual human review record and records that reviewer in the native approval.
   Do not use an administrator bypass: absent human approval evidence fails closed.
5. The **publish** summary links the PR. Download **publication-receipt-…** for the same
   execution/approval IDs, artifact checksum, reviewer, PR number, branch and commit SHA.
   Inspect the PR and leave it open as synthetic demonstration evidence; no merge is needed.

The publication job receives no model key and makes no inference or validator calls. It restores
the original Redis snapshot, verifies exact state/history and the pending native interrupt, then
submits the approval through the same HTTP route as the console. The two jobs check out the same
source SHA. The bundle is bound to this run, first attempt, repository and commit, and its digest
is passed directly between jobs. Only same-run artifacts are accepted. Never load arbitrary
third-party RDB files into a trusted Redis service.

The workflow is intentionally one Kubernetes scenario; use `combined-live-workflow` for both
Kubernetes and Terraform through review, without PR publication.

## Failure, rejection and cleanup

- Missing setup, quota errors, invalid generation or validation failure stop before PR writes.
  Two invocations is an upper bound, not a guarantee of success; provider quotas can change.
- A proposed correction after the two-call budget is rejected before another provider request.
  No HTTP retries, fallback model, automatic workflow reruns or apply commands are configured.
- Reject or cancel the pending environment review to prevent publication. This leaves the saved
  domain approval pending until the disposable data expires; it is not a native API rejection.
- Both artifacts expire after **one day**. Download evidence promptly. The bundle contains only
  synthetic data; never adapt this upload path to production incident logs or credentials.
- A failure after repository writes may leave a branch, commit or PR. Inspect GitHub before
  another run. First-attempt checks reject **Re-run jobs**; a newly dispatched run is a new
  incident and can create a different PR. Do not blindly dispatch another run to recover.
- No automatic cleanup deletes remote branches or PRs. Close synthetic PRs/delete their exact
  branches manually when no longer needed. Never merge a sample remediation just to clean it up.

## Optional local live readiness

Only on a machine where Redis and Docker are permitted:

```bash
agentic-remediation-check --mode live
agentic-remediation-check --mode live --probe-services
```

The first command checks configuration presence and deliberately returns not-ready because
services were not probed. The second performs read-only Docker daemon/image checks and Redis
PING, with bounded timeouts. Neither command calls a model, writes GitHub, initializes Redis
indices, prints secret values, checks token validity or verifies free-tier eligibility.
Build the validator images and configure credentials as described in the README before starting
`agentic-remediation-api`. Do not run service probes on a Docker-prohibited laptop.

## Verification ledger

| Check | Evidence and scope |
| --- | --- |
| Prior combined live inference/validation/recovery | [Run 39118745681](https://github.com/P-yushh/agentic-incident-remediation/actions/runs/39118745681), source `1f36018`: Kubernetes + Terraform, four Gemini invocations, real validators, Redis native review and fresh-process history recovery. No publication. |
| Prior real publisher | [PR #9](https://github.com/P-yushh/agentic-incident-remediation/pull/9): application approval/publisher with saved synthetic live-validated output. A separate publication-only test, not one continuous fresh-incident run. |
| New reviewed Actions workflow | Implemented with offline continuity/safety tests. A first real hosted run and its human review are still required; the two prior results do not prove this new workflow passed. |
| New RDB handoff test | Opt-in container CI test: fixture model, real kubeconform + Redis snapshot/restart, fresh-process approval and simulated publisher. No hosted inference or real GitHub writes. |

Automated tests intentionally use fixture models or mocked HTTP. Only a successfully completed
`reviewed-live-demo` run with its linked real PR establishes the new continuous live path.
