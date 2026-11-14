"""Which tests a pytest command selects, so a pass can stand in for a coverage-only failure.

rebench26 held-out run (2026-11-14): exploratory ``pytest tests/x.py
--cov=pkg.mod`` failed while collecting (numpy imported twice under coverage)
and the same file passed under plain pytest on the same bytes. The completion
gate still owed the ``--cov`` argv, so both repair turns went to "fixing"
coverage and ended with a ``.pth`` written into site-packages.

The grammar is an allowlist: a command is understood only when every option is
known to change reporting or coverage measurement and nothing else, it runs at
the repository root, and its environment assignments are coverage or display
settings. Anything else (``-W error``, ``-n``, ``--import-mode``, a plugin's
``--runslow``, ``-k``, a ``--`` separator, another directory) is not
understood, and a failure that is not understood stays owed exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

#: Long options that only change what is printed or measured. A coverage
#: threshold is deliberately absent: missing coverage is the run's own verdict.
_NEUTRAL_LONG = frozenset(
    {
        "--quiet", "--verbose", "--tb", "--color", "--durations", "--durations-min",
        "--no-header", "--no-summary", "--showlocals", "--full-trace", "--junitxml",
        "--junit-xml", "--cov", "--cov-report", "--cov-config", "--cov-branch",
        "--cov-append", "--no-cov", "--no-cov-on-fail", "--exitfirst", "--maxfail",
        "--capture", "--show-capture", "--disable-warnings", "--disable-pytest-warnings",
    }
)
#: Neutral long options whose value is the next argument when written without "=".
_LONG_VALUE = frozenset(
    {"--tb", "--color", "--durations", "--durations-min", "--junitxml", "--junit-xml",
     "--cov-report", "--cov-config", "--maxfail", "--capture", "--show-capture"}
)
#: Neutral short flags, and the neutral short options that take a value.
_SHORT_FLAGS = frozenset("qvslx")
_SHORT_VALUE = frozenset("r")
#: ``-p`` values that only switch bookkeeping off.
_HARMLESS_PLUGINS = frozenset({"no:cacheprovider"})
#: ``coverage run`` options before ``-m pytest``.
_COVERAGE_RUN = frozenset({"--branch", "--parallel-mode", "--append", "-a", "-p"})
_COVERAGE_RUN_VALUE = frozenset({"--source", "--rcfile", "--data-file", "--include", "--omit", "--context"})
#: Commands that may follow the test run in a ``cd ROOT && ...`` chain.
_REPORTING = re.compile(r"^(?:(?:python3?\s+-m\s+)?coverage\s+(?:report|html|xml|json|combine|erase)(?:\s+-m)?|true)$")
_SHELL_META = re.compile(r"[|;<>`$()\\'\"*?~\[\]{}]|&(?!&)")
_ENVIRONMENT = re.compile(r"^(?:COVERAGE_[A-Z_]*|NO_COLOR|FORCE_COLOR|COLUMNS|PYTHONDONTWRITEBYTECODE)=[^\s]*$")

#: (selected targets relative to the root; empty means the configured suite)
Selection = frozenset[str]


def _pytest_arguments(parts: list[str]) -> Optional[list[str]]:
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        if not _ENVIRONMENT.match(parts[0]):
            return None
        parts = parts[1:]
    if not parts:
        return None
    stem = Path(parts[0]).stem.lower()
    if stem == "pytest":
        return parts[1:]
    python = re.fullmatch(r"python(?:3(?:\.\d+)?)?", stem) is not None
    if python and parts[1:3] == ["-m", "pytest"]:
        return parts[3:]
    if python and parts[1:3] == ["-m", "coverage"]:
        rest = parts[3:]
    elif stem == "coverage":
        rest = parts[1:]
    else:
        return None
    if not rest or rest[0] != "run":
        return None
    index = 1
    while index < len(rest):
        item = rest[index]
        if item == "-m":
            return rest[index + 2 :] if rest[index + 1 : index + 2] == ["pytest"] else None
        name, has_value, _value = item.partition("=")
        if name in _COVERAGE_RUN_VALUE:
            index += 1 if has_value else 2
        elif item in _COVERAGE_RUN:
            index += 1
        else:
            return None
    return None


def _targets(arguments: Sequence[str], root: str) -> Optional[Selection]:
    targets: set[str] = set()
    index = 0
    while index < len(arguments):
        item = arguments[index]
        index += 1
        if item.startswith("--"):
            name, has_value, _value = item.partition("=")
            if name not in _NEUTRAL_LONG:
                return None
            if name in _LONG_VALUE and not has_value:
                index += 1
            continue
        if item.startswith("-") and len(item) > 1:
            if item[1] == "p":
                value = item[2:] or (arguments[index] if index < len(arguments) else "")
                index += 0 if item[2:] else 1
                if value not in _HARMLESS_PLUGINS:
                    return None
                continue
            for position, letter in enumerate(item[1:], start=2):
                if letter in _SHORT_VALUE:
                    if position == len(item):
                        index += 1
                    break
                if letter not in _SHORT_FLAGS:
                    return None
            continue
        target = item.replace("\\", "/")
        if target.startswith("/"):
            if not root or not (target == root or target.startswith(root + "/")):
                return None
            target = target[len(root) :]
        target = target.lstrip("/").removeprefix("./").rstrip("/")
        if ".." in target.split("/"):
            return None
        targets.add(target or ".")
    return frozenset() if "." in targets else frozenset(targets)


def selection(argv: Sequence[str], repository: str = "") -> Optional[Selection]:
    """The tests a pytest command at the repository root selects, or None when unsure."""

    root = repository.replace("\\", "/").rstrip("/")
    parts = [str(part) for part in argv]
    if len(parts) == 3 and Path(parts[0]).name in {"bash", "sh"} and parts[1] in {"-c", "-lc"}:
        commands = [piece.strip() for piece in parts[2].split("&&")]
        if any(not piece or _SHELL_META.search(piece) for piece in commands):
            return None
        if commands[0].startswith("cd "):
            directory = commands[0][3:].strip().rstrip("/")
            if directory not in {".", root} or not directory:
                return None
            commands = commands[1:]
        if not commands or any(_REPORTING.match(command) for command in commands[:1]):
            return None
        if not all(_REPORTING.match(command) for command in commands[1:]):
            return None
        parts = commands[0].split()
    arguments = _pytest_arguments(parts)
    if arguments is None:
        return None
    return _targets(arguments, root)


def _covers(wide: str, narrow: str) -> bool:
    return wide == narrow or narrow.startswith(wide + "/") or narrow.startswith(wide + "::")


def covered(failed: Selection, passes: Iterable[Selection]) -> bool:
    """Whether some pass selects every test the failed run selected."""

    for targets in passes:
        if not targets:
            return True
        if failed and all(any(_covers(wide, item) for wide in targets) for item in failed):
            return True
    return False


def measures_coverage(argv: Sequence[str]) -> bool:
    """Whether a command runs its tests under coverage (the case the discharge exists for)."""

    joined = " ".join(str(part) for part in argv)
    return bool(re.search(r"(?:^|\s)--cov(?:[=\s]|$)|\bcoverage\s+run\b", joined))


__all__ = ["Selection", "covered", "measures_coverage", "selection"]
