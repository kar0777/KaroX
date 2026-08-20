"""Run the canonical CI coverage gate and persist machine-readable evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
DATA_FILE = SCRATCH / ".coverage.current"
JSON_REPORT = SCRATCH / "coverage_current.json"
XML_REPORT = SCRATCH / "coverage_current.xml"


def _run(argv: list[str], env: dict[str, str]) -> int:
    print("+ " + subprocess.list2cmdline(argv), flush=True)
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        shell=False,
        check=False,
    )
    return int(completed.returncode)


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for path in (JSON_REPORT, XML_REPORT):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(DATA_FILE)
    python = sys.executable

    commands = [
        [python, "-m", "coverage", "erase"],
        [
            python,
            "-m",
            "coverage",
            "run",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ],
        [python, "-m", "coverage", "combine"],
        [python, "-m", "coverage", "json", "-o", str(JSON_REPORT)],
        [python, "-m", "coverage", "xml", "-o", str(XML_REPORT)],
    ]
    for argv in commands:
        code = _run(argv, env)
        if code != 0:
            return code

    payload = json.loads(JSON_REPORT.read_text(encoding="utf-8"))
    totals = payload.get("totals", {})
    percent = totals.get("percent_covered")
    print(f"COVERAGE_TOTAL={percent}", flush=True)

    # Keep the threshold enforcement last so JSON/XML evidence remains available
    # even when the gate is red.
    return _run([python, "-m", "coverage", "report"], env)


if __name__ == "__main__":
    raise SystemExit(main())
