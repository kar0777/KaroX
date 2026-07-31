#!/usr/bin/env python3
"""Run the KaroX 5 reviewed-fix and verification sequence.

This orchestrator exists because the ChatGPT KaroX connector used during the
release review exposes repository read/write and Git inspection, but not process
execution. It gives the developer one reproducible local command without hiding
which checks actually ran.

Examples:

    python scripts/run_v5_preflight.py --full
    python scripts/run_v5_preflight.py --static-only --json

The default runs repository/static contracts and focused bridge tests. ``--full``
also runs the complete suite, Ruff, Mypy, and coverage. The script never commits,
pushes, publishes, edits VERSION, or marks live conformance records passed.

It used to open with an ``--apply-reviewed-fixes`` stage that ran two one-time
migration helpers. Both fixes are in the source, both helpers are deleted, and
the release-hygiene gate refuses a shipping tree that still contains them, so the
stage would have nothing left to run.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
OUTPUT_TAIL_LIMIT = 12_000


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]
    required: bool = True


@dataclass
class StepResult:
    name: str
    argv: list[str]
    required: bool
    exit_code: int
    duration_seconds: float
    status: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_truncated: bool = False


STATIC_STEPS = (
    Step(
        "compile source and tests",
        (PYTHON, "-m", "compileall", "-q", "src", "tests", "scripts"),
    ),
    Step("dependency contract", (PYTHON, "scripts/check_dependencies.py")),
    Step("version and repository contracts", (PYTHON, "scripts/check_versions.py")),
    Step("published test counts", (PYTHON, "scripts/check_test_count.py")),
    Step(
        "KaroX 5 product contract",
        (PYTHON, "scripts/check_v5_release.py", "--json"),
    ),
    Step(
        "access-profile contract",
        (PYTHON, "scripts/check_access_profiles.py", "--json"),
    ),
    Step(
        "user-facing copy contract",
        (PYTHON, "scripts/check_user_facing_copy.py", "--json"),
    ),
    Step(
        "documented command contract",
        (PYTHON, "scripts/check_documented_commands.py", "--json"),
    ),
    Step(
        "installer preservation contract",
        (PYTHON, "scripts/check_installer_preservation.py", "--json"),
    ),
    Step(
        "release workflow contract",
        (PYTHON, "scripts/check_release_workflow.py", "--json"),
    ),
    Step(
        "development release hygiene",
        (PYTHON, "scripts/check_release_hygiene.py", "--json"),
    ),
    Step("Git whitespace check", ("git", "diff", "--check")),
)

FOCUSED_STEPS = (
    Step(
        "OAuth bridge focused tests",
        (
            PYTHON,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_oauth_bridge.py",
            "-v",
        ),
    ),
    Step(
        "web bridge launcher focused tests",
        (
            PYTHON,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_web_bridge_launcher.py",
            "-v",
        ),
    ),
)

FULL_STEPS = (
    Step("Ruff", (PYTHON, "-m", "ruff", "check", "src", "tests", "scripts")),
    Step("Mypy", (PYTHON, "-m", "mypy", "src/karox")),
    Step(
        "complete unittest suite",
        (
            PYTHON,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ),
    ),
    Step(
        "coverage run",
        (
            PYTHON,
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
        ),
    ),
    Step("coverage combine", (PYTHON, "-m", "coverage", "combine")),
    Step("coverage report", (PYTHON, "-m", "coverage", "report")),
)


def _display_argv(argv: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _bounded_tail(value: str) -> tuple[str, bool]:
    if len(value) <= OUTPUT_TAIL_LIMIT:
        return value, False
    return value[-OUTPUT_TAIL_LIMIT:], True


def _run_step(step: Step, *, json_mode: bool) -> StepResult:
    if not json_mode:
        print(f"\n==> {step.name}")
        print(f"    {_display_argv(step.argv)}")
    started = time.monotonic()
    completed = subprocess.run(
        step.argv,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=json_mode,
    )
    duration = time.monotonic() - started
    status = "passed" if completed.returncode == 0 else "failed"

    stdout_tail = ""
    stderr_tail = ""
    truncated = False
    if json_mode:
        stdout_tail, stdout_truncated = _bounded_tail(completed.stdout or "")
        stderr_tail, stderr_truncated = _bounded_tail(completed.stderr or "")
        truncated = stdout_truncated or stderr_truncated
    else:
        print(f"<== {status} ({duration:.2f}s, exit {completed.returncode})")

    return StepResult(
        name=step.name,
        argv=list(step.argv),
        required=step.required,
        exit_code=completed.returncode,
        duration_seconds=round(duration, 3),
        status=status,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_truncated=truncated,
    )


def _build_steps(args: argparse.Namespace) -> list[Step]:
    steps: list[Step] = []
    steps.extend(STATIC_STEPS)
    if not args.static_only:
        steps.extend(FOCUSED_STEPS)
    if args.full:
        steps.extend(FULL_STEPS)
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--static-only",
        action="store_true",
        help="run only compile/static/repository contracts",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="also run Ruff, Mypy, complete unittest suite, and coverage",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="continue after a required failure to collect a complete local report",
    )
    parser.add_argument("--json", action="store_true", help="emit one valid JSON report")
    args = parser.parse_args(argv)

    steps = _build_steps(args)
    results: list[StepResult] = []
    for step in steps:
        result = _run_step(step, json_mode=args.json)
        results.append(result)
        if result.exit_code != 0 and step.required and not args.keep_going:
            break

    failed = [item for item in results if item.required and item.exit_code != 0]
    not_run = len(steps) - len(results)
    summary = {
        "ok": not failed and not_run == 0,
        "root": str(ROOT),
        "python": PYTHON,
        "mode": "full" if args.full else "static" if args.static_only else "focused",
        "steps_planned": len(steps),
        "steps_run": len(results),
        "steps_not_run": not_run,
        "failed_required_steps": [item.name for item in failed],
        "results": [asdict(item) for item in results],
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("\n=== KaroX 5 preflight summary ===")
        print(f"mode: {summary['mode']}")
        print(f"steps: {len(results)}/{len(steps)}")
        if failed:
            print("failed:")
            for item in failed:
                print(f"  - {item.name} (exit {item.exit_code})")
        if not_run:
            print(f"not run after failure: {not_run}")
        if not failed and not_run == 0:
            print("all selected local steps passed")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
