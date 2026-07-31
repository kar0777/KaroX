#!/usr/bin/env python3
"""Validate the KaroX 5 release inventory and temporary-file hygiene.

Development branches may contain reviewed one-time migration/fix helpers while a
large exact transformation is waiting to be applied and tested. A final 5.0
shipping tree may not. It must contain the canonical release documents,
checkers, issue/PR templates, and no temporary apply helper or empty placeholder
test introduced solely for repository editing.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
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
    "docs/INSTALLER_REHEARSAL.md",
    "docs/MIGRATION_V4_TO_V5.md",
    "docs/CONNECTIVITY.md",
    "docs/IMPLEMENTATION_STATUS.md",
    "docs/conformance/README.md",
    "docs/conformance/beta-summary.md",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/beta-test.yml",
    "scripts/check_v5_release.py",
    "scripts/check_access_profiles.py",
    "scripts/check_user_facing_copy.py",
    "scripts/check_installer_preservation.py",
    "scripts/check_documented_commands.py",
    "scripts/check_release_workflow.py",
    "scripts/check_release_hygiene.py",
    "scripts/run_v5_preflight.py",
)

TEMPORARY_PATTERNS = (
    "scripts/apply_v5_*_fix.py",
    "scripts/apply_v5_*.py",
)

KNOWN_PLACEHOLDERS = (
    "tests/test_v5_release_gate.py",
)


def _runtime_version(root: Path) -> str:
    path = root / "src" / "karox" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.strip()
    raise ValueError("src/karox/__init__.py has no literal __version__")


def _shipping_version(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def _temporary_files(root: Path) -> list[str]:
    found: set[str] = set()
    for pattern in TEMPORARY_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    for relative in KNOWN_PLACEHOLDERS:
        path = root / relative
        if path.is_file():
            found.add(relative)
    return sorted(found)


def collect_problems(
    root: Path = ROOT,
    *,
    strict: bool = False,
) -> list[str]:
    problems: list[str] = []

    for relative in REQUIRED_FILES:
        path = root / relative
        if not path.is_file():
            problems.append(f"required KaroX 5 release file is missing: {relative}")
        elif path.stat().st_size == 0:
            problems.append(f"required KaroX 5 release file is empty: {relative}")

    try:
        runtime = _runtime_version(root)
    except (OSError, SyntaxError, ValueError) as exc:
        problems.append(f"cannot read runtime version: {type(exc).__name__}: {exc}")
        runtime = ""
    try:
        shipping = _shipping_version(root)
    except OSError as exc:
        problems.append(f"cannot read shipping version: {type(exc).__name__}: {exc}")
        shipping = ""

    final_tree = runtime == "5.0.0" or shipping == "5.0.0"
    temporary = _temporary_files(root)
    if strict or final_tree:
        for relative in temporary:
            problems.append(
                f"strict/final release contains temporary repository-edit file: {relative}"
            )

    archived = root / "docs" / "vNext" / "README.md"
    if not archived.is_file():
        problems.append("docs/vNext/README.md historical index is missing")
    else:
        text = archived.read_text(encoding="utf-8").lower()
        if "historical" not in text and "archived" not in text:
            problems.append("docs/vNext/README.md is not clearly marked historical")

    release_scope = root / "docs" / "V5_RELEASE_SCOPE.md"
    if release_scope.is_file():
        text = release_scope.read_text(encoding="utf-8")
        if runtime and runtime not in text:
            problems.append(
                f"docs/V5_RELEASE_SCOPE.md does not state runtime version {runtime!r}"
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="reject every temporary apply helper and placeholder test",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems = collect_problems(ROOT, strict=args.strict)
    temporary = _temporary_files(ROOT)
    payload = {
        "ok": not problems,
        "strict": args.strict,
        "temporary_files": temporary,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif problems:
        print("KaroX 5 release hygiene failed:")
        for problem in problems:
            print(f"  - {problem}")
        if temporary and not args.strict:
            print("temporary development files present:")
            for relative in temporary:
                print(f"  - {relative}")
    else:
        print("KaroX 5 release inventory and hygiene are consistent")
        if temporary:
            print("development tree still contains temporary files:")
            for relative in temporary:
                print(f"  - {relative}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
