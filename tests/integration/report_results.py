"""Publish bounded synthetic test failures from an ephemeral JUnit report."""

import sys
from pathlib import Path
from xml.etree import ElementTree


def report_results(path: Path) -> None:
    if not path.is_file():
        print("No integration report was generated; inspect the preceding job step.")
        return
    root = ElementTree.parse(path).getroot()
    cases = root.findall(".//testcase")
    failures = 0
    skipped = 0
    for case in cases:
        if case.find("skipped") is not None:
            skipped += 1
        for tag in ("failure", "error"):
            result = case.find(tag)
            if result is None:
                continue
            failures += 1
            message = f"{case.get('name')}\n{result.text or result.get('message', '')}"
            escaped = message[-7000:].replace("%", "%25")
            escaped = escaped.replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Container integration test failed::{escaped}", flush=True)
    print(f"Integration report: {len(cases)} executed, {failures} failures, {skipped} skipped.")


if __name__ == "__main__":
    report_results(Path(sys.argv[1]))
