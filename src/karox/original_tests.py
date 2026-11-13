"""Original tests against the changed code: what an edited test file hides.

Benchmark 2026-11-13 (click-3239, 4 of 4 failures): the change broke an
existing test case, the model edited that case to match, and every check it
ran was green -- on its own copy of the test. A grader runs the repository's
original tests (plus the task's), so the hidden regression was the whole
failure. Here the session's edited, pre-existing test files are swapped back to
their committed bytes, run against the changed source, and compared with the
same tests on the committed source: a case that passed before and fails now is
a regression the edit hid, unless the task itself changes that behaviour.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import check_baseline
from .completion_rules import _changed_existing_tests, _is_test_path
from .security import child_process_environment

RUN_TIMEOUT_SECONDS = 240.0
MAX_TEST_FILES = 8

_CACHE: Dict[str, Optional[List[str]]] = {}


def _committed(core: Any, relative: str) -> Optional[bytes]:
    shown = subprocess.run(
        ["git", "show", f"HEAD:{Path(relative).as_posix()}"],
        cwd=str(core.repository),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        env=child_process_environment(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return shown.stdout if shown.returncode == 0 else None


def _run_with(core: Any, files: Dict[str, Optional[bytes]], argv: List[str]) -> Optional[check_baseline.Outcome]:
    """Run ``argv`` with ``files`` (absolute path -> bytes) in place, then restore every byte."""

    saved = {path: (Path(path).read_bytes() if Path(path).is_file() else None) for path in files}
    journal = check_baseline.swap_journal_path(core.repository)
    check_baseline.recover_swap(journal)
    check_baseline.write_swap_journal(journal, saved)
    try:
        check_baseline.place(files)
        result = core._run(argv, RUN_TIMEOUT_SECONDS, keep_full=True)
    except Exception:  # noqa: BLE001 - restored below; no evidence either way
        result = None
    finally:
        check_baseline.place(saved)
        if not check_baseline.verify_placed(saved):
            raise RuntimeError(f"KaroX could not restore the working tree; the journal {journal} holds the bytes")
        journal.unlink(missing_ok=True)
    if not isinstance(result, dict):
        return None
    outcome = check_baseline.of_result(result)
    return outcome if outcome.parsed and not outcome.timed_out else None


def regressions(core: Any, record: Any) -> Optional[List[str]]:
    """Original test cases that pass on the committed code and fail on the changed code.

    ``None`` when there is nothing to compare (no edited pre-existing test file,
    no changed source, not pytest-shaped) or when a run gave no usable result.
    """

    tests = [p for p in _changed_existing_tests(core, record) if p.endswith(".py")][:MAX_TEST_FILES]
    sources = [
        p for p in dict.fromkeys(getattr(record, "changed_files", ()) or ())
        if isinstance(p, str) and p.endswith(".py") and not _is_test_path(p)
    ]
    if not tests or not sources:
        return None
    root = Path(core.repository)
    current: Dict[str, Optional[bytes]] = {}
    committed: Dict[str, Optional[bytes]] = {}
    for relative in [*tests, *sources]:
        path = root / relative
        current[str(path)] = path.read_bytes() if path.is_file() else None
        committed[str(path)] = _committed(core, relative)
    key = hashlib.sha256(repr(sorted((k, hashlib.sha256(v or b"").hexdigest()) for k, v in current.items())).encode()).hexdigest()
    if key in _CACHE:
        return _CACHE[key]
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *tests]
    originals = {str(root / t): committed[str(root / t)] for t in tests}
    after = _run_with(core, originals, argv)
    before = _run_with(core, committed, argv) if after is not None else None
    found: Optional[List[str]] = None
    if after is not None and before is not None:
        found = sorted(after.failing - before.failing)
    _CACHE[key] = found
    return found


def undeclared(found: List[str], superseded: frozenset[str]) -> List[str]:
    return [n for n in found if not any(n == d or n.endswith("::" + d) or n.split("::")[-1] == d for d in superseded)]


def reason(nodes: List[str]) -> str:
    shown = ", ".join(nodes[:6]) + (f" (+{len(nodes) - 6} more)" if len(nodes) > 6 else "")
    return (
        f"your code change makes {len(nodes)} original test case(s) fail that pass on the "
        f"committed code: {shown}. You edited their test file, but the original tests are "
        "what the project keeps: change the code so these pass again, unless the task "
        "explicitly changes exactly that behaviour -- then list the test in finish "
        "superseded_tests with the task sentence that changes it"
    )
