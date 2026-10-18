"""Run the real dashboard controller in Node's isolated deterministic DOM harness."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_incident_remediation.review_scenarios import SCENARIOS


def test_dashboard_controller_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the dependency-free dashboard controller tests")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("dashboard_controller.test.cjs"))],
        input=json.dumps([s.public() for s in SCENARIOS.values()]),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
