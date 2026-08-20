"""Explicit-only launcher for the detached managed coverage acceptance."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import coverage_managed_acceptance as acceptance  # noqa: E402


def test_launch_managed_coverage() -> None:
    result = ROOT / "scratch" / "coverage_managed_acceptance_latest.json"
    output = ROOT / "scratch" / "coverage_managed_acceptance_latest_output.txt"
    for path in (result, output):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    assert acceptance._wmi_launch(result, ROOT) == 0
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if result.exists():
            payload = json.loads(result.read_text(encoding="utf-8"))
            assert payload.get("status") in {"running", "passed"}
            assert payload.get("isolated", {}).get("profile") == (
                "chatgpt-autonomy-coverage-acceptance"
            )
            return
        time.sleep(0.2)
    raise AssertionError("managed coverage orchestrator did not publish initial evidence")
