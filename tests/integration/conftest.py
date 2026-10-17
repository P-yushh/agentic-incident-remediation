"""Surface bounded synthetic-test failures in the public Actions check annotations."""

import os

import pytest


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not report.failed or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    # This suite uses synthetic inputs and explicitly empty provider credentials.
    # Keep normal job logs intact, but make the immediate failure actionable too.
    message = f"{report.nodeid}\n{report.longreprtext[-6000:]}"
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title=Container integration test failed::{escaped}")
