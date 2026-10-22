"""Download checksum-verified native validation tools to a private benchmark directory."""

import argparse
import hashlib
import io
import json
import platform
import tarfile
import zipfile
from pathlib import Path

import httpx


def prepare(root: Path) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("this bootstrap is explicitly for native Apple Silicon validation")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    records = {}
    with httpx.Client(timeout=120, follow_redirects=True, trust_env=False) as client:

        def download(url: str) -> bytes:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 350_000_000:
                        raise ValueError("validator download exceeded the size limit")
            return bytes(data)

        def verified(base: str, name: str, sums: str) -> bytes:
            checksums = download(base + sums).decode().splitlines()
            expected = next(
                line.split()[0] for line in checksums if line.split()[-1].lstrip("*") == name
            )
            data = download(base + name)
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise ValueError("validator archive checksum mismatch")
            records[name] = actual
            return data

        kube = verified(
            "https://github.com/yannh/kubeconform/releases/download/v0.8.0/",
            "kubeconform-darwin-arm64.tar.gz",
            "CHECKSUMS",
        )
        with tarfile.open(fileobj=io.BytesIO(kube), mode="r:gz") as archive:
            source = archive.extractfile("kubeconform")
            if source is None:
                raise ValueError("kubeconform binary missing")
            (root / "kubeconform").write_bytes(source.read())
        terraform = verified(
            "https://releases.hashicorp.com/terraform/1.13.3/",
            "terraform_1.13.3_darwin_arm64.zip",
            "terraform_1.13.3_SHA256SUMS",
        )
        with zipfile.ZipFile(io.BytesIO(terraform)) as archive:
            (root / "terraform").write_bytes(archive.read("terraform"))
        provider = verified(
            "https://releases.hashicorp.com/terraform-provider-aws/6.2.0/",
            "terraform-provider-aws_6.2.0_darwin_arm64.zip",
            "terraform-provider-aws_6.2.0_SHA256SUMS",
        )
        target = root / "providers/registry.terraform.io/hashicorp/aws/6.2.0/darwin_arm64"
        target.mkdir(parents=True, mode=0o700)
        with zipfile.ZipFile(io.BytesIO(provider)) as archive:
            name = next(
                n
                for n in archive.namelist()
                if n.startswith("terraform-provider-aws_v") and "/" not in n
            )
            binary = target / name
            binary.write_bytes(archive.read(name))
            binary.chmod(0o700)
        schema = download(
            "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/v1.31.0-standalone-strict/deployment-apps-v1.json"
        )
        json.loads(schema)
        (root / "deployment-apps-v1.json").write_bytes(schema)
        records["deployment-apps-v1.json"] = hashlib.sha256(schema).hexdigest()
    for name in ("kubeconform", "terraform"):
        (root / name).chmod(0o700)
    (root / "tool-provenance.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps({"tool_directory": str(root), "verified_archives": records}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    prepare(parser.parse_args().directory)
