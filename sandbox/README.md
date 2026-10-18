# Validator images

These images contain the schema/provider material required to validate generated artifacts while
runtime networking is disabled.

Build them once from the repository root:

```bash
docker build \
  --tag agentic-remediation/kubeconform-validator:0.1.0 \
  sandbox/kubeconform-validator

docker build \
  --tag agentic-remediation/terraform-validator:0.1.0 \
  sandbox/terraform-validator
```

The application sends artifact content through standard input. It does not mount the repository,
Docker socket, cloud credentials, or user home directory into a validator. Runtime containers use
no network, a read-only root filesystem, a temporary 16 MiB `/tmp`, dropped Linux capabilities,
`no-new-privileges`, an unprivileged user, and CPU, memory, process, and wall-clock limits.

Image builds require network access to fetch the pinned Kubernetes schema and Terraform provider.
That build-time access is separate from artifact validation and should be moved to a trusted image
build pipeline with digest and checksum verification before production use.

The Kubernetes schema-download stage uses Alpine 3.22's current `curl` package. Do not pin an
individual APK revision here: the release repository replaces older revisions when security
updates are published, which can make a previously successful build fail. This deliberately
allows downloader-package updates within the selected Alpine release; it is not a bit-for-bit
reproducible image build. The kubeconform version and Kubernetes schema version remain fixed,
and artifact validation still runs without network access. The `container-integration` workflow
rebuilds the images and verifies both valid and invalid artifacts without hosted model calls.

The Terraform image preserves the unpacked provider mirror hierarchy under `/opt/providers`:
`registry.terraform.io/hashicorp/aws/<version>/<platform>/`. Do not flatten provider binaries into
the mirror root: current Terraform discovery requires the registry, namespace, version, and target
directories. Runtime initialization can link the read-only bundled provider into its temporary
working directory without downloading or unpacking a provider there.
