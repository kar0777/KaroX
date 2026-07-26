#!/usr/bin/env python3
"""Fail when a published test count disagrees with the suite.

``README.md``, ``docs/vNext/README.md`` and ``docs/vNext/IMPLEMENTATION_STATUS.md``
each advertise the size of the suite, and the number is the whole point: it is
offered as evidence a reader can reproduce. Three hand-written copies of a figure
that changes on every commit that adds a test is the same failure mode
``check_versions.py`` exists for -- and it had already happened, with all three
copies reading 536 against a suite of 576.

The count is not consolidated into one place because each document has a
legitimate reason to state it. It is checked instead, so a copy cannot go stale
quietly.

What is counted is what the documented runner runs:
``python -m unittest discover -s tests -p "test_*.py"``. That agrees with
``python -m pytest --collect-only -q tests`` and is deliberately not a pytest
*pass* tally, which moves between runs because pytest adds subtests on top of
tests.

Standard library only, but unlike ``check_versions.py`` this one imports the test
modules, so it needs the runtime's dependencies installed. ``--print`` emits the
number for CI to consume instead of repeating it in YAML.
"""

from __future__ import annotations

import argparse
import ast
import re
import unittest
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]

# The legacy KaroX 4 checks live outside ``tests`` and are swept up by a bare
# ``pytest`` at the repository root, which is why the two figures differ and why
# the documents explain the gap rather than pretending it away.
LEGACY_CHECKS = ROOT / "scripts" / "test_karox4_units.py"

# Each entry is a place a document states the suite size, with the pattern that
# finds it. A pattern that stops matching is reported too: a reworded sentence
# that quietly drops the number is the same problem as a stale one.
SUITE_COUNT_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"The suite is (\d+) tests"),
    ("README.md", r"`Ran (\d+) tests`"),
    ("README.md", r"^(\d+) is the number"),
    ("docs/vNext/README.md", r"`Ran (\d+) tests`"),
    ("docs/vNext/README.md", r"(\d+) is also what"),
    ("docs/vNext/IMPLEMENTATION_STATUS.md", r"Current (\d+)-test suite"),
    ("docs/vNext/IMPLEMENTATION_STATUS.md", r"Current local evidence is (\d+) tests"),
    ("docs/vNext/IMPLEMENTATION_STATUS.md", r"^  (\d+) is what"),
)

ROOT_COUNT_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"root collects (\d+)"),
    ("docs/vNext/README.md", r"root collects (\d+)"),
)


def _walk(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _walk(item)
        else:
            yield item  # type: ignore[misc]


def _discover(start: Path, pattern: str) -> list[unittest.TestCase]:
    suite = unittest.TestLoader().discover(str(start), pattern=pattern)
    return list(_walk(suite))


def _import_failures(cases: list[unittest.TestCase]) -> list[str]:
    """Name the modules discovery could not import.

    unittest turns an unimportable module into a single synthetic failing test,
    so it still *counts*. Left undetected, a module that stopped importing would
    look like a suite that had merely shrunk by one.
    """
    return [
        case.id() for case in cases if "_FailedTest" in type(case).__name__
    ]


def _legacy_check_count() -> int:
    """Count the legacy KaroX 4 checks the way pytest does.

    They are plain ``test_*`` functions rather than ``TestCase`` methods, so
    ``unittest`` discovery finds none of them -- which is precisely why a bare
    ``pytest`` at the repository root reports a larger number than the documented
    runner. Parsed rather than imported: this file is not part of the vNext suite
    and need not be importable for the counts to be checked.
    """
    if not LEGACY_CHECKS.is_file():
        return 0
    tree = ast.parse(LEGACY_CHECKS.read_text(encoding="utf-8"))
    return sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _stated(path: Path, pattern: str) -> int | None:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    return int(match.group(1)) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", choices=("suite", "root"), dest="emit")
    args = parser.parse_args(argv)

    cases = _discover(ROOT / "tests", "test_*.py")
    broken = _import_failures(cases)
    if broken:
        print("test modules failed to import, so no count is trustworthy:")
        for name in broken:
            print(f"  - {name}")
        return 1
    suite_count = len(cases)

    legacy_count = _legacy_check_count()
    root_count = suite_count + legacy_count

    if args.emit == "suite":
        print(suite_count)
        return 0
    if args.emit == "root":
        print(root_count)
        return 0

    problems: list[str] = []
    for claims, expected, label in (
        (SUITE_COUNT_CLAIMS, suite_count, "suite"),
        (ROOT_COUNT_CLAIMS, root_count, "repository-root collection"),
    ):
        for relative, pattern in claims:
            path = ROOT / relative
            if not path.is_file():
                problems.append(f"{relative} does not exist but is checked for a {label} count")
                continue
            stated = _stated(path, pattern)
            if stated is None:
                problems.append(
                    f"{relative} no longer states a {label} count matching {pattern!r}; "
                    "update the pattern in scripts/check_test_count.py if the wording changed "
                    "on purpose"
                )
            elif stated != expected:
                problems.append(
                    f"{relative} says {stated} for the {label} count but it is {expected}"
                )

    if problems:
        print("published test counts disagree with the suite:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"published test counts agree (suite {suite_count} under "
        f'`unittest discover -s tests -p "test_*.py"`, {root_count} collected at the '
        f"repository root including {legacy_count} legacy checks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
