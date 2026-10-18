# Container integration testing

The `container-integration` workflow verifies real Docker validation and Redis checkpoints without
requiring Docker on a developer's laptop. It runs on GitHub-hosted Ubuntu, not on a self-hosted
machine. All inputs are synthetic, model outputs are deterministic, and the publisher is a test
double that must receive no proposals. It neither calls a hosted model nor creates a remediation PR.

## Checks

| Check | Real dependency exercised |
| --- | --- |
| Valid Deployment accepted | Kubeconform with bundled Kubernetes schema |
| Invalid Deployment rejected with findings | Kubeconform schema validation |
| Valid security group rule accepted | Terraform with bundled AWS provider |
| Unsupported Terraform argument rejected with findings | Terraform provider schema validation |
| Kubernetes approval restored and rejected in a fresh process | Redis checkpoints, HTTP API, native approval interrupt |
| Terraform approval restored and rejected in a fresh process | Redis checkpoints, HTTP API, native approval interrupt |
| Paused incident restored, resumed, and terminated in a fresh process | Redis controls, real validation, HTTP API |

Checkpoint IDs are random per execution. The tests never flush Redis or remove another execution's
data. Use only the disposable Redis service provided by this job; the test helper requires a
loopback Redis URL. The service and its data disappear when the runner is discarded.

## Run on GitHub

The job runs for relevant pull requests, pushes to `main` and `codex/**` branches, and manual
dispatch. Once the workflow is on the default branch, open the repository's **Actions** tab,
select **container-integration**, and use **Run workflow**. The job log contains image build output
and each pytest result. A failed or skipped job is not evidence of a successful integration test.

No laptop configuration, repository secret, personal access token, hosted API credit, deployment,
or cloud account is needed for this workflow. Repository workflow permissions remain read-only.

## Cost safeguards

- Use a standard `ubuntu-24.04` GitHub-hosted runner, not a larger or self-hosted runner.
- The job is restricted to public repositories and skips if repository visibility changes.
- No Actions cache, artifact upload, container registry publication, or persistent cloud service.
- A 25-minute job limit and per-branch cancellation bound accidental repeated execution.
- No hosted model calls and no `secrets` references. GitHub, Hugging Face, and Gemini credentials
  are explicitly empty in the test process; checkout does not retain its temporary credentials.

GitHub documents standard hosted runner execution as free for public repositories:
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Artifact storage, larger runners, and external provider usage have separate billing rules; this
workflow does not enable or purchase them. This does not certify employer-policy approval.

## Optional local execution

Only on a machine where Docker is permitted, build the validator images documented in
[the sandbox guide](../sandbox/README.md), start a disposable Redis 8.2 service, and run:

```bash
AIR_RUN_CONTAINER_TESTS=1 \
AIR_INTEGRATION_REDIS_URL=redis://127.0.0.1:6379/0 \
python -m pytest tests/integration -m integration -vv
```

Without `AIR_RUN_CONTAINER_TESTS=1`, these tests skip, preserving the credential-free local suite.
Explicitly enabling them without the real dependencies fails rather than silently using doubles.

## Boundaries

These checks prove container validation and cross-process checkpoint recovery, not hosted-model
quality, Kubernetes runtime recovery or real GitHub publication. When the disposable Redis service
container ID is provided, an additional RDB snapshot/restart handoff test checks restoration of the
original review interrupt and approval in a fresh process, with a fixture model and fake publisher.
It is not a general production Redis durability or backup/restore guarantee.
Validators run with the production runner's network disabled, read-only root filesystem,
unprivileged user, no host mounts, and CPU/memory/process/time limits. They validate proposals;
they do not run `kubectl apply`, `terraform plan`, or `terraform apply`.

Hosted inference and real publication are excluded from these automated container checks.
Provider adapter and live-app wiring tests use mocked HTTP and synthetic keys;
they do not establish live inference availability or response quality. See the
[provider configuration](../README.md#selecting-gemini) for no-billing setup and boundaries.
The deterministic offline dashboard remains usable independently of all these services.

## Explicit combined live verification

`combined-live-workflow` is a separate, manually dispatched workflow. Normal CI and the
container-integration job still never call hosted models. To run it, an owner must store a Gemini
key as the repository Actions secret `GEMINI_API_KEY`, select `main`, and explicitly confirm the
live-call input. Use a provider project with billing unset and verify its current free allowance.
This workflow cannot configure provider billing, but the application is not a billing control.

The public-repository-only job uses an ephemeral standard Ubuntu runner and a disposable Redis
service. It builds both real validator images without model credentials, then sends two synthetic
incidents through the actual Gemini planner and generator, production Docker sandbox runner,
Redis checkpointer, API, and native human-review interrupt. Only that inference step receives the
Gemini secret. There is no real repository publisher or GitHub write token.

Each scenario has a hard two-call budget, shared between planning and artifact generation;
the entire run has at most four Gemini invocations. A correction attempt beyond that budget fails
closed before calling the provider. No HTTP retries, provider fallback, automatic workflow reruns,
artifact uploads, or image publication are configured. A failed job does not authorize another
live run: inspect its report before deliberately spending more provider quota.

A second Python process, without the Gemini key, restores both executions and checks the complete
checkpoint, validation records, pending approval, native interrupt, and history. It submits no
approval or rejection. A successful report therefore proves the combined pipeline up to human
review and process-restart recovery, not deployment, incident resolution, Redis-server restart
durability, or real remediation PR publication. All inputs and generated output are synthetic;
resource IDs and workload images are placeholders. Results stay in the job log, while report files
disappear with the runner. The API key must never appear in source, logs, or generated reports.

The harness is `scripts/combined_live_workflow.py`; its offline unit tests use in-memory
persistence, fixture models, and fixture validators and do not spend hosted-model quota.

## Reviewed fresh-incident-to-PR demonstration

The separate manual `reviewed-live-demo` workflow adds one continuous synthetic Kubernetes
execution through actual publication. It allows at most two Gemini invocations, then saves the
original Redis RDB, proposal and review evidence as a one-day artifact. A required GitHub
environment review separates generation from publication. The second job receives only the
repository job token, verifies GitHub's human approval history and the exact same-run bundle,
restores the original Redis checkpoints, and resumes the pending native approval via the API.
It never reconstructs an execution from an artifact, merges a PR or applies infrastructure.

Unlike the older combined verification, this workflow deliberately uploads **synthetic-only**
review evidence and a checkpoint snapshot. It requires explicit owner setup, cannot run as normal
CI, rejects job reruns, and does not automatically clean up published branches/PRs. See the
[demonstration runbook](demo-workflow.md) for setup, review, failure handling, readiness commands
and an evidence ledger separating prior verified runs from new unverified workflow code.
