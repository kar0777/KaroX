"""Verify that every published test-count claim matches discovery.

The count has been inflated accidentally before: a developer ran ``pytest`` at
repository root (which also discovers five legacy checks under ``scripts``) and
published that number as the application-suite count.  This gate uses the runner
we document, counts the root collection separately, and keeps both claims honest.

Historical phase-by-phase documents under ``docs/vNext`` intentionally retain
the counts that were true at those phase checkpoints. Only the archived vNext
landing page, which explicitly describes the current suite, remains a current
claim.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

# The five legacy KaroX 4 checks are plain pytest functions in one module, not
# unittest.TestCase methods, so `unittest` discovery cannot see them at all: the
# old glob `test_karox_v4*.py` matched no file on disk either, and the gate
# therefore reported 0 legacy tests and failed its own equality check. The count
# is taken by parsing that module for top-level `test_*` functions, which is what
# the bare pytest collection documented as "root collects 714" actually finds.
LEGACY_CHECKS = ROOT / "scripts" / "test_karox4_units.py"
EXPECTED_LEGACY_SCRIPT_TESTS = 5


@dataclass(frozen=True)
class Claim:
    relative_path: str
    pattern: str
    count_kind: str = "suite"

    @classmethod
    def coerce(cls, value: Any, count_kind: str) -> "Claim":
        """Accept a Claim or a bare ``(path, pattern)`` pair.

        The claim tables are the natural seam for a test to substitute a tiny
        fixture tree, and forcing it to build dataclasses adds nothing.
        """
        if isinstance(value, cls):
            return value
        path, pattern = value
        return cls(str(path), str(pattern), count_kind)


# These are current product claims. Archived phase records are not rewritten
# whenever the live suite grows; treating them as current would destroy useful
# historical evidence.
# Split by which number they quote. The root-collection claims describe the bare
# pytest collection at the repository root, which is the suite plus the legacy
# checks; everything else quotes the suite under `tests` alone. Keeping them in
# separate tables means a test can substitute one kind without silently changing
# the meaning of the other.
SUITE_COUNT_CLAIMS = (
    Claim("README.md", r"The suite is ([0-9]+) tests\."),
    Claim("README.md", r"A clean run reports `Ran ([0-9]+) tests`\."),
    Claim("docs/vNext/README.md", r"reported `Ran ([0-9]+) tests`\."),
    Claim("README_RU.md", r"Полный suite содержит ([0-9]+) тестов\."),
    Claim(
        "docs/IMPLEMENTATION_STATUS.md",
        r"canonical documented suite is now ([0-9]+) tests",
    ),
    Claim(
        "docs/IMPLEMENTATION_STATUS.md",
        r"run the complete ([0-9]+)-test suite",
    ),
    Claim(
        "docs/RELEASE_CHECKLIST.md",
        r"Complete ([0-9]+)-test suite passes",
    ),
)

# `\s+` rather than a literal space: both sentences wrap across a newline in the
# rendered documents, so a single-space pattern silently matched nothing and the
# root-collection figure went unchecked.
ROOT_COUNT_CLAIMS = (
    Claim(
        "README.md",
        r"repository root collects ([0-9]+) because",
        "root",
    ),
    Claim(
        "docs/vNext/README.md",
        r"repository root collects ([0-9]+) because",
        "root",
    ),
)


def _discover(start_dir: Path, pattern: str) -> list[Any]:
    """Collect test cases exactly as the documented runner does.

    ``top_level_dir=ROOT`` used to be passed here, which made ``unittest`` import
    ``tests`` as a package. There is no ``tests/__init__.py``, so discovery died
    with "Start directory is not importable" and this gate could not run at all --
    while CI's own ``python -m unittest discover -s tests`` worked, because it
    passes no top-level directory. Matching the documented invocation keeps the
    number this gate publishes and the number CI reports the same number.
    """
    previous = list(sys.path)
    # Discovery must import the checkout under test, not an older installed wheel.
    # Some test modules import `karox` directly before `_support` gets a chance to
    # prepend src/, which otherwise poisons sys.modules for the rest of discovery.
    sys.path[:0] = [str(start_dir), str(SRC)]
    try:
        suite = unittest.defaultTestLoader.discover(str(start_dir), pattern=pattern)
    finally:
        sys.path[:] = previous
    return _flatten(suite)


def _flatten(suite: unittest.TestSuite) -> list[Any]:
    cases: list[Any] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(_flatten(item))
        else:
            cases.append(item)
    return cases


def _import_failures(cases: Iterable[Any]) -> list[str]:
    """Name the modules unittest replaced with a synthetic failing placeholder.

    An unimportable test module still contributes exactly one "test", so a broken
    module makes the total look slightly small rather than wrong. Reporting the
    placeholders by name turns that silent drift into an actionable message.
    """
    identifiers: list[str] = []
    for case in cases:
        # A real discovered case always has id(); anything else is a stand-in and
        # cannot be an import placeholder, so it is skipped rather than crashing
        # a release gate on an attribute it does not need.
        identify = getattr(case, "id", None)
        if not callable(identify):
            continue
        identifier = str(identify())
        if "unittest.loader._FailedTest" in identifier:
            identifiers.append(identifier)
    return sorted(identifiers)


def _legacy_check_count(path: Path) -> int:
    """Count top-level ``test_*`` functions in a pytest-style module.

    Parsed rather than imported: this module is a script, and a release gate must
    not execute repository code to find out how many checks it declares.
    """
    if not path.exists():
        return 0
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def discovered_count(start_dir: Path, pattern: str) -> int:
    return len(_discover(start_dir, pattern))


def _stated(claim: Claim) -> int:
    path = ROOT / claim.relative_path
    if not path.exists():
        raise RuntimeError(f"missing documented-count file: {claim.relative_path}")
    text = path.read_text(encoding="utf-8")
    match = re.search(claim.pattern, text)
    if match is None:
        raise RuntimeError(
            f"missing documented-count claim in {claim.relative_path}: {claim.pattern}"
        )
    return int(match.group(1))


def _rewrite(claim: Claim, expected: int) -> Optional[str]:
    """Update one documented claim in place, returning what changed.

    Every claim pattern captures the number in group 1, so the substitution can
    be derived from the match rather than from a second copy of the sentence. The
    surrounding prose is untouched: only the digits inside the group move.
    """
    path = ROOT / claim.relative_path
    # ``newline=""`` on both sides: the default translates line endings on read
    # and again on write, so updating four digits in a repository checked out with
    # CRLF would rewrite every line of the file on Windows and none of them on
    # Linux. Reading the bytes as they are keeps the change to the digits.
    with path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    match = re.search(claim.pattern, text)
    if match is None:
        return None
    stated = int(match.group(1))
    if stated == expected:
        return None
    start, end = match.span(1)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text[:start] + str(expected) + text[end:])
    return (
        f"{claim.relative_path}: {claim.count_kind} count {stated} -> {expected}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    # Lets a maintainer read a single number without parsing the report, and lets
    # a test assert the gate runs clean without depending on its prose.
    parser.add_argument("--print", dest="print_kind", choices=("suite", "root", "legacy"))
    # The suite grows in most commits that add a test, so the documented copies
    # go stale constantly and were being retyped by hand across five documents --
    # which is its own source of a wrong number. This applies discovery's figure
    # to the claims the gate already knows how to find. It deliberately does not
    # touch anything else the gate reports: an unimportable module or a changed
    # legacy count still has to be looked at by a person.
    parser.add_argument(
        "--write",
        action="store_true",
        help="update stale documented counts in place instead of only reporting them",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    cases = _discover(TESTS, "test_*.py")
    suite_count = len(cases)
    legacy_count = _legacy_check_count(LEGACY_CHECKS)
    root_count = suite_count + legacy_count

    issues: list[str] = []
    broken = _import_failures(cases)
    if broken:
        issues.append(
            "test modules that no longer import: " + ", ".join(broken)
        )
    if legacy_count != EXPECTED_LEGACY_SCRIPT_TESTS:
        issues.append(
            "legacy script test count changed: "
            f"expected {EXPECTED_LEGACY_SCRIPT_TESTS}, observed {legacy_count}; "
            "review root-collection documentation and this gate"
        )

    claims = [
        *(Claim.coerce(item, "suite") for item in SUITE_COUNT_CLAIMS),
        *(Claim.coerce(item, "root") for item in ROOT_COUNT_CLAIMS),
    ]
    updates: list[str] = []
    for claim in claims:
        expected = root_count if claim.count_kind == "root" else suite_count
        if args.write:
            try:
                changed = _rewrite(claim, expected)
            except OSError as exc:
                issues.append(f"cannot update {claim.relative_path}: {exc}")
                continue
            if changed is not None:
                updates.append(changed)
        try:
            stated = _stated(claim)
        except RuntimeError as exc:
            issues.append(str(exc))
            continue
        if stated != expected:
            issues.append(
                f"{claim.relative_path} states {stated} {claim.count_kind} tests; "
                f"discovery reports {expected}"
            )

    payload = {
        "ok": not issues,
        "suite_count": suite_count,
        "legacy_script_count": legacy_count,
        "root_count": root_count,
        "issues": issues,
        "updates": updates,
    }
    if args.print_kind:
        print(
            {
                "suite": suite_count,
                "root": root_count,
                "legacy": legacy_count,
            }[args.print_kind]
        )
    elif args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"suite tests: {suite_count}; legacy script checks: {legacy_count}; "
            f"root collection: {root_count}"
        )
        for update in updates:
            print(f"+ {update}")
        for issue in issues:
            print(f"- {issue}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
