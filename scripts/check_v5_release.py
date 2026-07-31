#!/usr/bin/env python3
"""Validate the KaroX 5 product contract and release evidence.

Development builds may keep required live records and external beta pending. A
strict rehearsal or final ``5.0.0`` shipping version may not. The gate uses only
the standard library so it can run before project dependencies are installed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_RECORDS = {
    "chatgpt-web.md": "chatgpt-web",
    "claude-web.md": "claude-web",
    "openai-responses.md": "openai-responses",
    "anthropic-messages.md": "anthropic-messages",
    "gemini.md": "gemini",
    "openai-compatible.md": "openai-compatible",
}

REQUIRED_DOCUMENTS = (
    "README.md",
    "README_RU.md",
    "QUICKSTART.md",
    "SECURITY.md",
    "TROUBLESHOOTING.md",
    "CONTRIBUTING.md",
    "docs/V5_RELEASE_SCOPE.md",
    "docs/RELEASE_CHECKLIST.md",
    "docs/BETA_TEST_PLAN.md",
    "docs/LIVE_TEST_RUNBOOK.md",
    "docs/MIGRATION_V4_TO_V5.md",
    "docs/CONNECTIVITY.md",
    "docs/IMPLEMENTATION_STATUS.md",
    "docs/conformance/README.md",
    "docs/conformance/beta-summary.md",
)

REQUIRED_CHECKERS = (
    "scripts/check_v5_release.py",
    "scripts/check_access_profiles.py",
    "scripts/check_release_workflow.py",
)

VALID_STATUSES = {"pending", "passed", "failed", "not-applicable"}
PASSED_FIELDS = (
    "verified_at_utc",
    "karox_version",
    "karox_commit",
    "platform",
    "external_version",
    "evidence",
)
BETA_THRESHOLDS = {
    "unaided_installation_attempts": 5,
    "completed_primary_scenarios": 3,
    "completed_without_maintainer": 2,
    "second_task_users": 2,
}
_PLACEHOLDERS = {"", "pending", "todo", "tbd", "none", "n/a"}
_STALE_CHATGPT_COPY = (
    "Settings → Plugins",
    "Настройки → Плагины",
)


def _read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _runtime_version(root: Path) -> str:
    source = _read(root, "src/karox/__init__.py")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value.strip()
    raise ValueError("src/karox/__init__.py has no literal __version__")


def _parse_record(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([a-z][a-z0-9_]*):\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def _placeholder(value: str | None) -> bool:
    return value is None or value.strip().lower() in _PLACEHOLDERS


def _integer(
    fields: dict[str, str],
    name: str,
    problems: list[str],
    source: str,
) -> int:
    value = fields.get(name)
    try:
        result = int(value or "")
    except ValueError:
        problems.append(
            f"{source} field {name!r} must be an integer, found {value!r}"
        )
        return -1
    if result < 0:
        problems.append(f"{source} field {name!r} cannot be negative")
    return result


def _final_v5(version: str) -> bool:
    return version == "5.0.0"


def collect_problems(
    root: Path = ROOT,
    *,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    blockers: list[str] = []

    for relative in REQUIRED_DOCUMENTS:
        path = root / relative
        if not path.is_file():
            problems.append(f"required document is missing: {relative}")
        elif not path.read_text(encoding="utf-8").strip():
            problems.append(f"required document is empty: {relative}")

    for relative in REQUIRED_CHECKERS:
        path = root / relative
        if not path.is_file():
            problems.append(f"required release checker is missing: {relative}")

    try:
        runtime = _runtime_version(root)
    except (OSError, SyntaxError, ValueError) as exc:
        problems.append(f"cannot read runtime version: {type(exc).__name__}: {exc}")
        runtime = ""

    try:
        release = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        problems.append(f"cannot read VERSION: {type(exc).__name__}: {exc}")
        release = ""

    if runtime and not runtime.startswith("5."):
        problems.append(f"KaroX 5 gate found runtime version {runtime!r}")

    final_requested = _final_v5(runtime) or _final_v5(release)
    if release.startswith("5.") and runtime != release:
        problems.append(
            f"shipping VERSION is {release!r} but runtime version is {runtime!r}; "
            "a KaroX 5 release must tag the exact packaged runtime"
        )

    readme_path = root / "README.md"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        if not readme.startswith("# KaroX 5"):
            problems.append("README.md must start with '# KaroX 5'")
        if runtime and runtime not in readme:
            problems.append(f"README.md does not state runtime version {runtime!r}")
        for link in (
            "docs/V5_RELEASE_SCOPE.md",
            "docs/RELEASE_CHECKLIST.md",
            "docs/BETA_TEST_PLAN.md",
            "docs/LIVE_TEST_RUNBOOK.md",
            "docs/MIGRATION_V4_TO_V5.md",
            "docs/CONNECTIVITY.md",
            "docs/IMPLEMENTATION_STATUS.md",
            "docs/conformance/README.md",
        ):
            if link not in readme:
                problems.append(f"README.md does not link {link}")
        if "operating-system sandbox" not in readme:
            problems.append(
                "README.md must state that KaroX is not an operating-system sandbox"
            )

    russian_path = root / "README_RU.md"
    if russian_path.is_file():
        russian = russian_path.read_text(encoding="utf-8")
        if "# KaroX 5" not in russian:
            problems.append("README_RU.md must identify KaroX 5")
        if runtime and runtime not in russian:
            problems.append(f"README_RU.md does not state runtime version {runtime!r}")
        for stale in ("v3.12.0", "Что нового в KaroX 3.12"):
            if stale in russian:
                problems.append(
                    f"README_RU.md still contains stale release claim {stale!r}"
                )
        for link in (
            "docs/V5_RELEASE_SCOPE.md",
            "docs/LIVE_TEST_RUNBOOK.md",
            "docs/CONNECTIVITY.md",
            "docs/IMPLEMENTATION_STATUS.md",
        ):
            if link not in russian:
                problems.append(f"README_RU.md does not link {link}")

    quick_path = root / "QUICKSTART.md"
    if quick_path.is_file():
        quick = quick_path.read_text(encoding="utf-8").lower()
        for required in ("chatgpt", "claude", "karox"):
            if required not in quick:
                problems.append(f"QUICKSTART.md does not cover {required}")

    scope_path = root / "docs" / "V5_RELEASE_SCOPE.md"
    if scope_path.is_file():
        scope = scope_path.read_text(encoding="utf-8")
        if runtime and runtime not in scope:
            problems.append(f"release scope does not state runtime version {runtime!r}")
        for heading in (
            "## Product promise",
            "## Primary release scenario",
            "## Shipping scope",
            "## Deferred beyond 5.0",
            "## P0 release blockers",
            "## Feature freeze rule",
        ):
            if heading not in scope:
                problems.append(f"release scope is missing heading {heading!r}")

    current_status = root / "docs" / "IMPLEMENTATION_STATUS.md"
    if current_status.is_file() and runtime:
        if runtime not in current_status.read_text(encoding="utf-8"):
            problems.append(f"docs/IMPLEMENTATION_STATUS.md does not state {runtime!r}")

    launcher_path = root / "src" / "karox" / "web_bridge_launcher.py"
    if launcher_path.is_file():
        launcher = launcher_path.read_text(encoding="utf-8")
        for stale in _STALE_CHATGPT_COPY:
            if stale in launcher:
                problems.append(
                    f"runtime ChatGPT instructions still contain stale UI path {stale!r}; "
                    "apply and test scripts/apply_v5_connection_copy_fix.py"
                )

    conformance = root / "docs" / "conformance"
    statuses: dict[str, str] = {}
    for filename, expected_integration in REQUIRED_RECORDS.items():
        path = conformance / filename
        relative = f"docs/conformance/{filename}"
        if not path.is_file():
            problems.append(f"required conformance record is missing: {relative}")
            continue
        fields = _parse_record(path.read_text(encoding="utf-8"))
        integration = fields.get("integration")
        status = fields.get("status", "")
        statuses[expected_integration] = status

        if integration != expected_integration:
            problems.append(
                f"{relative} integration is {integration!r}, "
                f"expected {expected_integration!r}"
            )
        if status not in VALID_STATUSES:
            problems.append(
                f"{relative} status is {status!r}; "
                f"expected one of {sorted(VALID_STATUSES)}"
            )
        if fields.get("release_blocker", "").lower() != "true":
            problems.append(f"{relative} must declare release_blocker: true")
        if runtime and fields.get("karox_version") != runtime:
            problems.append(
                f"{relative} karox_version is {fields.get('karox_version')!r}, "
                f"expected {runtime!r}"
            )
        if status == "passed":
            missing = [
                name for name in PASSED_FIELDS if _placeholder(fields.get(name))
            ]
            if missing:
                problems.append(
                    f"{relative} is passed but has placeholder fields: "
                    + ", ".join(missing)
                )
        elif status in {"pending", "failed"}:
            blockers.append(f"{expected_integration}: {status}")
        elif status == "not-applicable":
            blockers.append(
                f"{expected_integration}: not-applicable requires scope review"
            )

    beta_path = conformance / "beta-summary.md"
    beta_fields: dict[str, str] = {}
    beta_status = "missing"
    if beta_path.is_file():
        beta_fields = _parse_record(beta_path.read_text(encoding="utf-8"))
        beta_status = beta_fields.get("status", "")
        if beta_status not in {"pending", "passed", "failed"}:
            problems.append(
                "docs/conformance/beta-summary.md status must be pending, "
                "passed, or failed"
            )
        if runtime and beta_fields.get("runtime_version") != runtime:
            problems.append(
                "docs/conformance/beta-summary.md runtime_version is "
                f"{beta_fields.get('runtime_version')!r}, expected {runtime!r}"
            )
        if beta_status != "passed":
            blockers.append(f"external-beta: {beta_status or 'missing'}")

    require_complete = strict or final_requested
    if require_complete:
        for integration, status in statuses.items():
            if status != "passed":
                problems.append(
                    f"release requires {integration} status 'passed', found {status!r}"
                )
        if beta_status != "passed":
            problems.append(
                f"release requires external beta status 'passed', found {beta_status!r}"
            )
        for field, minimum in BETA_THRESHOLDS.items():
            value = _integer(beta_fields, field, problems, "beta-summary")
            if value >= 0 and value < minimum:
                problems.append(
                    f"external beta requires {field} >= {minimum}, found {value}"
                )
        open_p0 = _integer(
            beta_fields,
            "open_p0_issues",
            problems,
            "beta-summary",
        )
        if open_p0 > 0:
            problems.append(f"external beta still has {open_p0} open P0 issue(s)")

        helper = root / "scripts" / "apply_v5_connection_copy_fix.py"
        if helper.exists():
            problems.append(
                "strict/final release still contains one-time "
                "scripts/apply_v5_connection_copy_fix.py; apply, verify, and remove it"
            )

    if final_requested:
        if runtime != "5.0.0" or release != "5.0.0":
            problems.append(
                f"final KaroX 5 requires runtime and VERSION both '5.0.0', found "
                f"runtime={runtime!r}, VERSION={release!r}"
            )
        notes = root / "RELEASE_NOTES_v5.0.0.md"
        if not notes.is_file():
            problems.append("final KaroX 5 has no RELEASE_NOTES_v5.0.0.md")
        checklist = root / "docs" / "RELEASE_CHECKLIST.md"
        if checklist.is_file():
            checklist_text = checklist.read_text(encoding="utf-8")
            if "NOT READY" in checklist_text:
                problems.append("release checklist still declares NOT READY")

    return problems, blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require all live conformance and external beta gates to pass",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable report",
    )
    parser.add_argument(
        "--print",
        choices=("runtime", "blockers"),
        dest="emit",
        help="print one value for scripts",
    )
    args = parser.parse_args(argv)

    try:
        runtime = _runtime_version(ROOT)
    except (OSError, SyntaxError, ValueError) as exc:
        runtime = f"error:{type(exc).__name__}"

    problems, blockers = collect_problems(ROOT, strict=args.strict)

    if args.emit == "runtime":
        print(runtime)
        return 0 if not runtime.startswith("error:") else 1
    if args.emit == "blockers":
        for blocker in blockers:
            print(blocker)
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not problems,
                    "runtime_version": runtime,
                    "strict": args.strict,
                    "problems": problems,
                    "release_blockers": blockers,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif problems:
        print("KaroX 5 release contract failed:")
        for problem in problems:
            print(f"  - {problem}")
        if blockers:
            print("release evidence still open:")
            for blocker in blockers:
                print(f"  - {blocker}")
    else:
        print(f"KaroX 5 release contract is internally consistent for {runtime}")
        if blockers:
            print("development build; release evidence remains open:")
            for blocker in blockers:
                print(f"  - {blocker}")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
