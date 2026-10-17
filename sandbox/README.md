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
