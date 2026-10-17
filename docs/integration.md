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
quality, Kubernetes runtime recovery, Redis service-restart durability, or real GitHub publication.
Validators run with the production runner's network disabled, read-only root filesystem,
unprivileged user, no host mounts, and CPU/memory/process/time limits. They validate proposals;
they do not run `kubectl apply`, `terraform plan`, or `terraform apply`.

Gemini or Hugging Face inference and an explicitly human-approved test PR remain separate live
integration stages. Provider adapter and live-app wiring tests use mocked HTTP and synthetic keys;
they do not establish live inference availability or response quality. See the
[provider configuration](../README.md#selecting-gemini) for no-billing setup and boundaries.
The deterministic offline dashboard remains usable independently of all these services.
