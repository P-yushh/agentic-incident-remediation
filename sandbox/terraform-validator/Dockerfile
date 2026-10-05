FROM hashicorp/terraform:1.13.3 AS providers

ARG AWS_PROVIDER_VERSION=6.2.0
WORKDIR /bootstrap
RUN printf 'terraform {\n  required_providers {\n    aws = {\n      source = "hashicorp/aws"\n      version = "=%s"\n    }\n  }\n}\n' "${AWS_PROVIDER_VERSION}" > versions.tf \
    && terraform init -backend=false -input=false -no-color \
    && mkdir -p /provider-cache \
    && find .terraform/providers -type f -name 'terraform-provider-aws*' \
      -exec cp '{}' /provider-cache/ ';'

FROM hashicorp/terraform:1.13.3

COPY --from=providers /provider-cache /opt/providers
COPY validate.sh /usr/local/bin/validate-artifact
RUN chmod 0555 /usr/local/bin/validate-artifact

ENV HOME=/tmp
ENTRYPOINT ["/usr/local/bin/validate-artifact"]
