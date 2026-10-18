"""Build-policy regression checks; real image behavior is verified by container-integration."""

import re
from pathlib import Path


def test_schema_downloader_does_not_pin_a_rotating_alpine_package_revision() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "sandbox" / "kubeconform-validator" / "Dockerfile"
    ).read_text()
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    # The release and validator remain constrained; only the disposable downloader's
    # APK revision can track security updates in the release repository.
    assert "FROM alpine:3.22 AS schemas" in instructions
    assert "FROM ghcr.io/yannh/kubeconform:v0.8.0-alpine" in instructions
    install = re.search(r"RUN apk add --no-cache ([^\\\n]+)", instructions)
    assert install is not None
    assert install.group(1).strip() == "curl"
    assert "curl --fail --silent --show-error --location" in instructions
    assert '"-schema-location", "file:///schemas/' in instructions
