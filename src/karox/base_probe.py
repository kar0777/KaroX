"""Run the agent's own regression tests against the unchanged base, safely.

Agentless measured the single largest gain of its pipeline (+5 points on
SWE-bench Lite) from keeping only reproduction tests that FAIL on the original
code. Karo's edge-case review asked the model to do this by hand, and the model
either skipped it or was tempted to stash/reset the working tree -- which, with
other agents attached over MCP, can leave a user's tree broken if interrupted.

This module does it mechanically and never touches the working tree: it adds a
detached `git worktree` of HEAD in a temporary directory OUTSIDE the
repository, copies only the session's changed test files into it, runs pytest on
them there, and removes the worktree. The result is a fact for the model
("your tests pass on the base too, so they do not detect the bug"), never a
verdict on its own: it can only add a reason to keep working.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = ["BaseProbeResult", "split_tests", "probe_tests_on_base", "describe"]

#: pytest exit codes: 0 all passed, 1 some failed, 2 interrupted/collection
#: error, 5 nothing collected.
_PASSED, _FAILED, _COLLECTION, _EMPTY = 0, 1, 2, 5


@dataclass(frozen=True)
class BaseProbeResult:
    status: str  # "fails_on_base" | "passes_on_base" | "error_on_base" | "skipped"
    tests: Tuple[str, ...] = ()
    exit_code: Optional[int] = None
    detail: str = ""
    seconds: float = 0.0
    tail: str = field(default="", repr=False)


def _is_test_path(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.suffix != ".py":
        return False
    name = p.name
    if name == "conftest.py":
        return False
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or any(part in {"tests", "test"} for part in p.parts[:-1])
    )


def split_tests(changed: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(test files, non-test files) among the session's changed paths."""

    tests: List[str] = []
    other: List[str] = []
    for path in changed:
        if not isinstance(path, str) or not path:
            continue
        (tests if _is_test_path(path) else other).append(path)
    return tests, other


def _no_window_kwargs() -> dict:
    if os.name == "nt":
        return {
            "creationflags": int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        }
    return {"start_new_session": True}


def _run(argv: Sequence[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_no_window_kwargs(),
    )


def probe_tests_on_base(
    repository: Path,
    changed_files: Sequence[str],
    *,
    python: Sequence[str] = ("python",),
    timeout_seconds: float = 120.0,
) -> BaseProbeResult:
    """Run the session's changed tests on a clean checkout of HEAD."""

    started = time.monotonic()
    repository = Path(repository).resolve()
    tests, sources = split_tests(changed_files)
    if not tests:
        return BaseProbeResult("skipped", detail="no changed test files")
    if not sources:
        return BaseProbeResult("skipped", tuple(tests), detail="only tests changed")
    present = [t for t in tests if (repository / t).is_file()]
    if not present:
        return BaseProbeResult("skipped", tuple(tests), detail="test files were deleted")
    for rel in present:
        resolved = (repository / rel).resolve()
        try:
            resolved.relative_to(repository)
        except ValueError:
            return BaseProbeResult("skipped", tuple(present), detail="test path escapes repository")
    temp_root = Path(tempfile.mkdtemp(prefix="karox-base-"))
    worktree = temp_root / "base"
    added = False
    try:
        try:
            add = _run(
                ["git", "worktree", "add", "--detach", "--quiet", str(worktree), "HEAD"],
                repository,
                min(60.0, timeout_seconds),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BaseProbeResult("skipped", tuple(present), detail=f"worktree failed: {type(exc).__name__}")
        if add.returncode != 0:
            return BaseProbeResult("skipped", tuple(present), detail="worktree failed: " + add.stdout.strip()[-200:])
        added = True
        for rel in present:
            target = worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / rel, target)
        remaining = max(10.0, timeout_seconds - (time.monotonic() - started))
        argv = [*python, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider", *present]
        try:
            run = _run(argv, worktree, remaining)
        except subprocess.TimeoutExpired:
            return BaseProbeResult("skipped", tuple(present), detail="timed out on base",
                                   seconds=time.monotonic() - started)
        except OSError as exc:
            return BaseProbeResult("skipped", tuple(present), detail=f"could not run pytest: {exc}")
        tail = run.stdout[-1500:]
        code = run.returncode
        if code == _FAILED:
            status = "fails_on_base"
        elif code == _PASSED:
            status = "passes_on_base"
        elif code == _COLLECTION:
            # A test importing a symbol the fix adds fails to collect on base:
            # that is still red on base, but a weaker signal -- say which.
            status = "error_on_base"
        else:
            status = "skipped"
        return BaseProbeResult(status, tuple(present), code, "", time.monotonic() - started, tail)
    finally:
        if added:
            try:
                _run(["git", "worktree", "remove", "--force", str(worktree)], repository, 60.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                _run(["git", "worktree", "prune"], repository, 60.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        shutil.rmtree(temp_root, ignore_errors=True)


def describe(result: BaseProbeResult) -> str:
    """One short paragraph for the model, or "" when there is nothing to say."""

    files = ", ".join(result.tests)
    if result.status == "passes_on_base":
        return (
            "KaroX ran your changed tests (" + files + ") on a clean checkout of the "
            "unchanged base: they PASS there too. They therefore do not detect the "
            "reported bug. Add or fix a test that fails on the base and passes with "
            "your change before claiming the fix is verified."
        )
    if result.status == "fails_on_base":
        return (
            "KaroX ran your changed tests (" + files + ") on a clean checkout of the "
            "unchanged base: they FAIL there and pass with your change, so they "
            "detect the bug. This does not prove every requirement is covered."
        )
    if result.status == "error_on_base":
        return (
            "KaroX ran your changed tests (" + files + ") on a clean checkout of the "
            "unchanged base: they could not even be collected there (exit 2), so "
            "they do not show a behavioural red-to-green. Prefer a test that fails "
            "on an assertion, not on an import."
        )
    return ""
