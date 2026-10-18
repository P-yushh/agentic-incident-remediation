"""Opt-in real Redis RDB handoff; synthetic model and simulated publication only."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AIR_RUN_CONTAINER_TESTS") != "1"
        or not os.environ.get("AIR_REDIS_CONTAINER"),
        reason="Requires opted-in disposable CI Redis service container and validator images",
    ),
]


@pytest.mark.parametrize(
    "scenario", ["kubernetes_oom_killed", "kubernetes_crash_loop", "terraform_drift"]
)
def test_original_review_survives_rdb_handoff_and_separate_publication_process(tmp_path, scenario):
    assert os.environ["AIR_INTEGRATION_REDIS_URL"] == "redis://127.0.0.1:6379/0"
    script = Path(__file__).parents[2] / "scripts/reviewed_live_demo.py"
    spec = importlib.util.spec_from_file_location("review_demo_handoff", script)
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    worker = Path(__file__).with_name("review_demo_worker.py")
    manifest = tmp_path / "manifest.json"
    prepared = subprocess.run(
        [sys.executable, str(worker), "prepare", str(manifest), "--scenario", scenario],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    demo.snapshot_redis(tmp_path)
    assert (tmp_path / "dump.rdb").stat().st_size > 0
    demo.restore_redis(tmp_path)
    published = subprocess.run(
        [sys.executable, str(worker), "publish", str(manifest)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert published.returncode == 0, published.stderr
    assert json.loads(published.stdout)["status"] == "completed"
