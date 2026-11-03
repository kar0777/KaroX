"""Cheap deterministic checks the harness runs after a successful change.

Editing code blind is the most expensive habit a coding agent has: a missing
brace costs a whole extra model turn to notice. These checks cost a few hundred
milliseconds of local CPU, run in parallel, and their result travels back inside
the same tool result the model is already reading -- so the mistake is caught in
the turn that caused it.

They are deliberately NOT verification evidence. A syntax check is not the
user's acceptance criteria, and treating it as proof of completion would
reintroduce exactly the false success the evidence chain exists to refuse.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

__all__ = ["QuickCheck", "quick_checks", "summarize"]

MAX_WORKERS = 4
SUMMARY_CHARS = 300
PYTHON_EXTENSIONS = frozenset({".py"})
NODE_EXTENSIONS = frozenset({".js", ".mjs", ".cjs"})
TYPESCRIPT_EXTENSIONS = frozenset({".ts", ".tsx"})
HTML_EXTENSIONS = frozenset({".html", ".htm"})
LSP_MIN_TIMEOUT_S = 1.0
CONCISE_LINE = re.compile(
    r"^.+?:(?P<line>\d+):\d+:\s*(?P<code>[A-Za-z]+[0-9]+)\b(?P<message>.*)$"
)
MAX_LISTED_ISSUES = 5


@dataclass(frozen=True)
class QuickCheck:
    """One check's verdict, kept short enough to travel inside a tool result."""

    name: str
    ok: bool
    summary: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "summary": self.summary}


def summarize(checks: Sequence[QuickCheck]) -> str:
    """The one line appended to the tool result the model reads."""
    return "quick checks: " + " · ".join(
        f"{check.name} {check.summary}" for check in checks
    )


def quick_checks(
    repository: Path,
    changed_paths: Sequence[str],
    *,
    timeout_s: float = 5.0,
) -> list[QuickCheck]:
    """Run the checks the changed files call for, in parallel, or none of them.

    A check that could not finish inside the shared budget is reported as
    ``skipped: timeout`` with ``ok=True``: not knowing is not a failure, and a
    harness that turns its own impatience into a red flag teaches the model to
    chase ghosts.
    """

    if timeout_s <= 0:
        raise ValueError("quick check timeout must be positive")
    targets: list[tuple[str, Callable[[Path, float], QuickCheck], Path]] = []
    for relative in _changed(changed_paths):
        target = _existing(repository, relative)
        if target is None:
            continue
        targets.extend(
            (name, runner, target) for name, runner in _select_checks(target)
        )
    if not targets:
        return []

    results: dict[int, QuickCheck] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {
        pool.submit(runner, target, timeout_s): (index, name)
        for index, (name, runner, target) in enumerate(targets)
    }
    done, pending = concurrent.futures.wait(futures, timeout=timeout_s)
    for future in done:
        index, name = futures[future]
        try:
            results[index] = future.result()
        except Exception as exc:  # pragma: no cover - a runner must not crash a run
            results[index] = QuickCheck(name, True, f"skipped: {type(exc).__name__}")
    for future in pending:
        index, name = futures[future]
        results[index] = QuickCheck(name, True, "skipped: timeout")
        future.cancel()
    # The pool is abandoned rather than joined: joining would wait exactly as
    # long as the checks that just missed the budget, which is the one thing the
    # budget exists to prevent. Each runner bounds its own subprocess by the same
    # timeout, so the abandoned threads end on their own.
    pool.shutdown(wait=False, cancel_futures=True)
    return [results[index] for index in sorted(results)]


def _changed(changed_paths: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in changed_paths:
        if isinstance(item, str) and item and item not in seen:
            seen.append(item)
    return seen


def _existing(repository: Path, relative: str) -> Optional[Path]:
    """The changed file inside the repository, or ``None`` if it is not there."""
    candidate = (repository / relative).resolve(strict=False)
    try:
        candidate.relative_to(repository.expanduser().resolve(strict=False))
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _select_checks(path: Path) -> list[tuple[str, Callable[[Path, float], QuickCheck]]]:
    """The checks one file's extension calls for, in a stable order."""
    suffix = path.suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        runners: list[tuple[str, Callable[[Path, float], QuickCheck]]] = [
            ("py_compile", _py_compile),
        ]
        if importlib.util.find_spec("ruff") is not None:
            runners.append(("ruff", _ruff))
        return runners
    if suffix in NODE_EXTENSIONS:
        return [("node_check", _node_check)] if shutil.which("node") else []
    if suffix in TYPESCRIPT_EXTENSIONS:
        # A .ts verdict needs a language server on PATH; when there is none the
        # file gets no quick check rather than a fabricated one.
        from .lsp_diagnostics import _spec_for

        try:
            _spec_for(path)
        except Exception:
            return []
        return [("lsp", _lsp)]
    if suffix in HTML_EXTENSIONS:
        return [("web_check", _web_check)]
    return []


def quick_check_families() -> tuple[str, ...]:
    """What an edit triggers, named for the prompt.

    Derived from the same extension sets and tool probes ``_select_checks``
    uses, so the sentence a model is told cannot drift from the checks that
    actually run: a prompt that promises a check nobody runs teaches the model
    to wait for feedback that never arrives.
    """

    families: list[str] = []
    if PYTHON_EXTENSIONS:
        names = ["py_compile"]
        if importlib.util.find_spec("ruff") is not None:
            names.append("ruff")
        families.append(f"{' and '.join(names)} on changed .py files")
    if NODE_EXTENSIONS and shutil.which("node"):
        families.append("node --check on changed JS files")
    if HTML_EXTENSIONS:
        families.append("a browser render check on changed .html pages")
    if TYPESCRIPT_EXTENSIONS:
        families.append("a language server on changed .ts files")
    return tuple(families)


def _python() -> str:
    return str(sys.executable)


def _bounded(
    argv: Sequence[str],
    cwd: Path,
    timeout_s: float,
    *,
    stdin_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        input=stdin_text,
        stdin=None if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(0.05, timeout_s),
        creationflags=flags,
        check=False,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:SUMMARY_CHARS]
    return ""


def _py_compile(path: Path, timeout_s: float) -> QuickCheck:
    # Syntax only, in process: nothing is imported or executed and no
    # __pycache__ is written. A subprocess `python -m py_compile` failed before
    # it ever looked at the file when the isolated venv could not import runpy
    # (seen in parallel Harbor runs), and the harness then refused a correct
    # change for missing structural evidence. The shared quick-check budget
    # still bounds this call through the pool wait in quick_checks().
    try:
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        return QuickCheck("py_compile", False, _first_line(str(exc)) or "failed")
    except (OSError, ValueError) as exc:
        return QuickCheck("py_compile", True, f"skipped: {type(exc).__name__}")
    return QuickCheck("py_compile", True, "ok")


def _ruff(path: Path, timeout_s: float) -> QuickCheck:
    started = time.monotonic()
    try:
        completed = _bounded(
            [_python(), "-m", "ruff", "check", "--output-format", "concise", str(path)],
            path.parent,
            timeout_s,
        )
    except subprocess.TimeoutExpired:
        return QuickCheck("ruff", True, "skipped: timeout")
    if completed.returncode == 0:
        return QuickCheck("ruff", True, "ok")
    issues = _ruff_issues(completed.stdout)
    if not issues:
        # An error ruff could not attribute to a line (a config problem, a
        # crashed run) still has to reach the model as a failure, not as "ok".
        return QuickCheck("ruff", False, _first_line(completed.stdout) or "failed")
    # Only what the change introduced is the change's problem. swe-requests
    # had an unused import at line 29 long before the edit; reporting it as a
    # failed quick check kept the completion floor shut on a correct patch and
    # cost two repair loops and two full-suite reruns. Issues are matched to
    # the committed file by code and message, never by line: an edit above
    # the old issue moves it without changing it.
    baseline = _ruff_baseline(path, timeout_s - (time.monotonic() - started))
    if baseline is not None:
        remaining = Counter(baseline)
        introduced: list[tuple[str, str, str]] = []
        for issue in issues:
            key = (issue[0], issue[2])
            if remaining[key] > 0:
                remaining[key] -= 1
                continue
            introduced.append(issue)
        if not introduced:
            return QuickCheck(
                "ruff", True, f"ok ({len(issues)} pre-existing issue(s), none new)"
            )
        listed = ", ".join(
            f"{code} line {line}" for code, line, _ in introduced[:MAX_LISTED_ISSUES]
        )
        summary = f"{len(introduced)} new issues: {listed}"
        if len(introduced) < len(issues):
            summary += f" ({len(issues) - len(introduced)} pre-existing not counted)"
        return QuickCheck("ruff", False, summary[:SUMMARY_CHARS])
    listed = ", ".join(f"{code} line {line}" for code, line, _ in issues[:MAX_LISTED_ISSUES])
    return QuickCheck("ruff", False, f"{len(issues)} issues: {listed}"[:SUMMARY_CHARS])


def _ruff_issues(output: str) -> list[tuple[str, str, str]]:
    """(code, line, message) for every concise ruff line."""
    issues: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        match = CONCISE_LINE.match(line.strip())
        if match:
            issues.append(
                (
                    match.group("code"),
                    match.group("line"),
                    " ".join(match.group("message").split()),
                )
            )
    return issues


def _ruff_baseline(path: Path, timeout_s: float) -> Optional[list[tuple[str, str]]]:
    """The (code, message) issues of the committed version of ``path``.

    ``None`` means "no baseline could be read" (not a git checkout, git or the
    budget missing) and keeps every issue counted. A file git does not know
    yet has an empty baseline: everything in a new file is new.
    """

    if timeout_s <= 0.05 or shutil.which("git") is None:
        return None
    started = time.monotonic()
    try:
        inside = _bounded(
            ["git", "rev-parse", "--is-inside-work-tree"], path.parent, timeout_s
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    try:
        shown = subprocess.run(
            ["git", "show", f"HEAD:./{path.name}"],
            cwd=str(path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.05, timeout_s - (time.monotonic() - started)),
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if shown.returncode != 0:
        return []
    committed = shown.stdout.decode("utf-8", errors="replace")
    left = timeout_s - (time.monotonic() - started)
    if left <= 0.05:
        return None
    try:
        completed = _bounded(
            [
                _python(),
                "-m",
                "ruff",
                "check",
                "--output-format",
                "concise",
                "--stdin-filename",
                str(path),
                "-",
            ],
            path.parent,
            left,
            stdin_text=committed,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode == 0:
        return []
    issues = _ruff_issues(completed.stdout)
    if not issues:
        return None
    return [(code, message) for code, _, message in issues]


def _node_check(path: Path, timeout_s: float) -> QuickCheck:
    node = shutil.which("node")
    if not node:
        return QuickCheck("node_check", True, "skipped: node not installed")
    try:
        completed = _bounded([node, "--check", str(path)], path.parent, timeout_s)
    except subprocess.TimeoutExpired:
        return QuickCheck("node_check", True, "skipped: timeout")
    if completed.returncode == 0:
        return QuickCheck("node_check", True, "ok")
    return QuickCheck("node_check", False, _first_line(completed.stdout) or "failed")


def _web_check(path: Path, timeout_s: float) -> QuickCheck:
    del timeout_s
    from .web_check import check_page

    try:
        problems = check_page(path)
    except Exception as exc:
        return QuickCheck("web_check", True, f"skipped: {type(exc).__name__}")
    if not problems:
        return QuickCheck("web_check", True, "ok")
    return QuickCheck(
        "web_check", False, f"{len(problems)} problems: {'; '.join(problems)}"[:SUMMARY_CHARS]
    )


def _lsp(path: Path, timeout_s: float) -> QuickCheck:
    from .lsp_diagnostics import LspDiagnostics, LspDiagnosticsError

    if timeout_s < LSP_MIN_TIMEOUT_S:
        return QuickCheck("lsp", True, "skipped: timeout")
    try:
        payload = LspDiagnostics(path.parent).diagnose(
            path.name, timeout_seconds=min(timeout_s, 30.0)
        )
    except LspDiagnosticsError as exc:
        return QuickCheck("lsp", True, f"skipped: {str(exc)[:SUMMARY_CHARS]}")
    count = int(payload.get("diagnostic_count") or 0)
    if not count:
        return QuickCheck("lsp", True, "ok")
    rows = payload.get("diagnostics") or []
    listed = ", ".join(
        f"{item.get('code') or item.get('source') or 'issue'} line {item.get('line')}"
        for item in rows[:MAX_LISTED_ISSUES]
        if isinstance(item, dict)
    )
    return QuickCheck("lsp", False, f"{count} diagnostics: {listed}"[:SUMMARY_CHARS])
