"""Pre-existing pytest failures: rerun a failed check on the session's start bytes
(HARD12 2026-11-10: a red-before-edit suite made the model "repair" unrelated code).
Fail-closed: same argv/exit code, no timeout, >0 and >= passed, no new failing id."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

# Parameters may hold " - " ("t[a - b]"): the node keeps its whole bracket.
_SUMMARY_ROW = re.compile(r"^(?:FAILED|ERROR) (?P<node>[^\s\[]+(?:\[(?:[^\[\]]|\[[^\]]*\])*\])?\S*)(?: - .*)?$", re.M)
_COUNT = re.compile(r"(?P<n>\d+) (?P<kind>passed|failed|errors?)\b")
_FINAL = re.compile(r"^=*\s*\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b.*\bin [\d.]+s", re.M)
#: A pytest progress row: one mark per test, then the running percentage.
_PROGRESS = re.compile(r"^(?P<marks>[.FEsxX]+)\s*\[\s*(?P<pct>\d+)%\]\s*$", re.M)
BASELINE_TIMEOUT_SECONDS = 900.0  # a suite slower than this gets no baseline
SWAP_MAX_CHECK_SECONDS = 300.0  # only checks this fast get an on-demand rerun
SWAP_MAX_FILES = 50
SWAP_MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    timed_out: bool
    failing: frozenset[str]
    passed: int
    parsed: bool
    #: failed + errors from the final line: every one must have a parsed id.
    failed: int = 0


def is_pytest(argv: Sequence[str]) -> bool:
    parts = [str(p) for p in argv]
    exe = Path(parts[0]).stem.lower() if parts else ""
    if exe in {"pytest", "py.test"}:
        return True
    return exe.startswith("python") and parts[1:3] == ["-m", "pytest"]


_SEGMENT = re.compile(r"&&|\|\||[;|\n]")
_QUIET = re.compile(r"\s*(?:[12]?>\s*/dev/null|2>&1)")


def _shell_missing_tool(script: str, text: str) -> str:
    """`bash -lc "python -m ruff format --check a; python -m ruff check a"`: every
    command of the chain is a tool the output reports missing (rebench26 v604h
    cpp-linter: two repair turns spent looking for ruff in /root/.cache)."""
    tools: list[str] = []
    for piece in (_QUIET.sub("", p).strip() for p in _SEGMENT.split(script)):
        try:
            tokens = shlex.split(piece)
        except ValueError:
            return ""
        if tokens and tokens[0] in {"echo", "printf"} and re.search(r"not found|No module named", piece):
            return ""  # the chain prints the very message itself (codex review)
        if not tokens or tokens[0] in {"cd", "head", "tail", "grep", "sort", "uniq", "wc", "tee", "echo", "true"}:
            continue  # moving and filtering output run no tool of their own
        py_m = Path(tokens[0]).stem.lower().startswith("python") and tokens[1:2] == ["-m"] and len(tokens) > 2
        tools.append(tokens[2].split(".")[0] if py_m else Path(tokens[0]).name)
    gone = [
        tool for tool in tools
        if re.search(rf"(?:No module named '?{re.escape(tool)}(?![\w.])|\b{re.escape(tool)}: (?:command )?not found)", text)
    ]
    return tools[0] if tools and len(gone) == len(tools) else ""


def missing_tool(argv: Sequence[str], result: Mapping[str, Any]) -> str:
    """The tool a failed check could not even start (`No module named ruff`, exit 127): no evidence either way."""
    parts, text = [str(p) for p in argv], f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    if len(parts) == 3 and Path(parts[0]).name in {"bash", "sh"} and parts[1] in {"-c", "-lc"}:
        if result.get("exit_code") in (0, None) or result.get("timed_out") or len(text) > 4000:
            return ""
        return _shell_missing_tool(parts[2], text)
    code, py_m = result.get("exit_code"), Path(parts[0] if parts else "").stem.lower().startswith("python") and parts[1:2] == ["-m"]
    if code in (0, None) or result.get("timed_out") or not parts or len(text) > 4000 or (code != 127 and not (py_m and len(parts) > 2)):
        return ""
    mod = Path(parts[0]).name if code == 127 else parts[2].split(".")[0]
    return mod if code == 127 or re.search(rf"No module named '?{re.escape(mod)}(?![\w.])", text) else ""


_QUOTE_FOLD = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "\\": None, "*": None, "`": None})
_QUOTED = re.compile(r'"([^"]{20,})"')
_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+|\n+")


def _fold(text: str) -> str:
    return " ".join(text.translate(_QUOTE_FOLD).split()).lower()


def _quotes_task(reason: str, task: str, sentences: Sequence[str]) -> bool:
    """The reason carries a verbatim piece of the task.

    Models wrap the quote ('The task specifies: "..." This case expects ...')
    and JSON-escape its inner quotes; requiring the whole reason to be task
    text refused a correct declaration on click-3239 (v50, both attempts).
    """

    r = _fold(reason)
    if len(r) < 20:
        return False
    if r in task:
        return True
    if any(len(q.strip()) >= 20 and q.strip() in task for q in _QUOTED.findall(r)):
        return True
    return any(sentence in r for sentence in sentences)


def superseded(record: Any) -> frozenset[str]:
    """Tests the last finish call declares outdated by the task, each with a verbatim task quote."""
    raw_task = str(getattr(record, "task", ""))
    task, calls = _fold(raw_task), []
    sentences = [f for piece in _SENTENCE_END.split(raw_task) if len(f := _fold(piece)) >= 20]
    for entry in getattr(record, "provider_history", None) or ():
        calls += [c for c in (entry.get("tool_calls") or ()) if isinstance(c, Mapping) and c.get("name") == "finish"]
    try:
        raw = calls[-1].get("raw_arguments") if calls else None
        items = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("superseded_tests") or []
    except (TypeError, ValueError, AttributeError):
        return frozenset()
    quoted = (e for e in items if isinstance(e, Mapping) and str(e.get("test") or "").strip())
    return frozenset(str(e["test"]).strip() for e in quoted
                     if _quotes_task(str(e.get("reason") or ""), task, sentences))


def only_superseded(data: Mapping[str, Any], names: frozenset[str]) -> bool:
    """Every NEW failure versus the start bytes is a declared superseded test (pytest only).

    A test the task names that still fails is unfinished work, whatever the
    new failures are: declaring an unrelated new failure superseded used to
    pass a run whose named target was still red.
    """
    raw = data.get("baseline")
    info: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    new = info.get("new") or []
    if info.get("task_targets_failing"):
        return False
    return bool(names and new) and all(any(n == d or n.endswith("::" + d) for d in names) for n in new)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def outcome(text: str, exit_code: Any, timed_out: Any) -> Outcome:
    # Colour codes (pygmt duel1: --color=yes in the project's pytest config)
    # hid every summary row, so no baseline was ever taken.
    text = _ANSI.sub("", text or "")
    nodes = (m.group("node").strip() for m in _SUMMARY_ROW.finditer(text or ""))
    finals = list(_FINAL.finditer(text or ""))
    counts = [(m.group("kind"), int(m.group("n"))) for m in _COUNT.finditer(finals[-1].group(0))] if finals else []
    passed = sum(n for kind, n in counts if kind == "passed")
    failed = sum(n for kind, n in counts if kind != "passed")
    # Under -qq (a project's ``addopts = "-q"`` plus the run's own -q) pytest
    # prints no final count line: the progress rows are the only count. Missing
    # them, rebench26 mtplx (2026-11-15) took no baseline, owed three failures
    # that were red before any edit, and the model skipped them in a test file.
    rows = [] if finals else list(_PROGRESS.finditer(text or ""))
    complete = bool(rows) and rows[-1].group("pct") == "100"
    if complete:
        marks = "".join(m.group("marks") for m in rows)
        passed, failed = marks.count("."), marks.count("F") + marks.count("E")
    code = int(exit_code) if isinstance(exit_code, int) or str(exit_code).lstrip("-").isdigit() else -1
    failing = frozenset(n for n in nodes if "::" in n or ".py" in n)
    return Outcome(code, bool(timed_out), failing, passed, bool(finals) or complete, failed)


def quiet_count(data: Mapping[str, Any]) -> str:
    """"N passed, M failed" for a pytest run that printed no count line (-qq), else "".

    The progress rows are all such a run shows; naming the count gives the
    model a line to quote instead of re-running the tests verbosely.
    """
    if not is_pytest([str(p) for p in data.get("argv") or ()]):
        return ""
    text = _ANSI.sub("", str(data.get("stdout") or ""))
    if _FINAL.search(text):
        return ""
    found = outcome(text, data.get("exit_code"), data.get("timed_out"))
    return f"{found.passed} passed, {found.failed} failed" if found.parsed else ""


def of_result(result: Mapping[str, Any]) -> Outcome:
    streams = (result.get(k) for k in ("_map_stdout", "stdout", "_map_stderr", "stderr"))
    out, std, err_full, err = (str(v or "") for v in streams)
    return outcome((out or std) + "\n" + (err_full or err), result.get("exit_code"), result.get("timed_out"))


_TEST_NAME = re.compile(r"\btest_\w+")


def task_test_names(task: str) -> frozenset[str]:
    """Test function names written in the task: the tests it asks to make pass."""
    return frozenset(_TEST_NAME.findall(task or ""))


def _named(node: str, names: frozenset[str]) -> bool:
    return node.split("::")[-1].split("[")[0] in names


def argv_without(argv: Sequence[str], absent: set[str]) -> Optional[list[str]]:
    """The check's argv for the start bytes: test files the session created are dropped.

    rebench26 v617rb hermes-webui-1818: ``pytest tests/new_test.py tests/old_test.py``
    could not run at the start revision (file not found, exit 4), so the
    old file's failures, a missing ``node`` binary, were never seen as
    pre-existing; 13 repair turns ended with a skip added to that old test.
    None when nothing named is left: a bare ``pytest`` would run the whole suite.
    """

    kept: list[str] = []
    dropped = False
    for item in argv:
        text = str(item)
        path = text.split("::", 1)[0].replace("\\", "/")
        if (path[2:] if path.startswith("./") else path) in absent:
            dropped = True
            continue
        kept.append(text)
    if not dropped:
        return list(argv)
    named = [item for item in kept[1:] if not item.startswith("-") and ("/" in item or ".py" in item)]
    return kept if named else None


def compare(
    baseline: Optional[Outcome], current: Outcome, task_tests: frozenset[str] = frozenset()
) -> Optional[dict[str, Any]]:
    """What the model and the gate are told about a failed run, or None."""
    if baseline is None or current.exit_code == 0 or not current.failing:
        return None
    if not (baseline.parsed and current.parsed) or baseline.timed_out or current.timed_out:
        return None
    new = sorted(current.failing - baseline.failing)
    # A test the task names that failed at the start is the task's target:
    # still red means unfinished, never "pre-existing, do not touch".
    targets = sorted(n for n in current.failing & baseline.failing if _named(n, task_tests))
    return {
        "baseline_exit_code": baseline.exit_code,
        "baseline_failing": len(baseline.failing),
        "baseline_passed": baseline.passed,
        "preexisting": sorted(current.failing & baseline.failing),
        "new": new,
        "task_targets_failing": targets,
        "preexisting_only": not new
        and not targets
        # A failure whose id the summary rows did not yield could be the new one.
        and len(current.failing) >= current.failed
        and current.exit_code == baseline.exit_code
        # A node-id rerun of failures only passes nothing on either side.
        and (0 < current.passed or baseline.passed == 0)
        and baseline.passed <= current.passed,
    }


def render(data: Mapping[str, Any]) -> str:
    info = data.get("baseline")
    if not isinstance(info, Mapping):
        return ""
    pre, new = list(info.get("preexisting") or []), list(info.get("new") or [])
    if info.get("preexisting_only"):
        return (
            f"[karox baseline] All {len(pre)} failing tests already failed on the files as they "
            "were before your edits (pre-existing, environment or upstream). No new failures: "
            "this check counts as passing. Do NOT edit unrelated code, tests or configuration "
            "to make them pass."
        )
    targets = list(info.get("task_targets_failing") or [])
    if targets and not new:
        return "\n".join(
            [f"[karox baseline] {len(targets)} failing test(s) the task names still fail. They failed "
             "before your edits too, but they are the task's targets: make them pass."]
            + ["  target: " + node for node in targets[:10]]
        )
    lines = [f"[karox baseline] {len(new)} failure(s) are NEW versus the files before your edits; "
             f"{len(pre)} pre-existing failure(s) are not yours to fix and can be ignored. A new failure "
             "that only asserts the old behaviour the task changes is superseded: keep the fix, list it in finish superseded_tests."]
    lines += ["  new: " + node for node in new[:10]]
    return "\n".join(lines + ([f"  ... {len(new) - 10} more new"] if len(new) > 10 else []))


# -- on-demand swap: changed files at start bytes, journaled outside the repo --


def swap_journal_path(repository: Path) -> Path:
    digest = hashlib.sha256(str(Path(repository).resolve()).encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / "karox-baseline-swap" / f"{digest}.json"


def _digest(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else hashlib.sha256(data).hexdigest()


#: Swap phases. Only while the check runs on placed start bytes can a path
#: that no longer holds them be someone's later edit; a crash while placing or
#: restoring leaves half-written files, and those must get the run's bytes back.
PHASE_PLACING, PHASE_RUNNING, PHASE_RESTORING = "placing", "running", "restoring"
#: A swap never outlives its rerun by this much: an older lock is stale even
#: when its PID now belongs to another live process.
_LOCK_STALE_SECONDS = BASELINE_TIMEOUT_SECONDS + 300.0


def write_swap_journal(
    journal: Path,
    saved: Mapping[str, Optional[bytes]],
    placed: Optional[Mapping[str, Optional[bytes]]] = None,
    phase: str = PHASE_PLACING,
) -> None:
    """Journal the run's bytes (and the swap's phase) before anything moves.

    ``placed`` records what the swap writes, by digest: a recovery after a
    crash in the running phase restores only the paths still holding exactly
    that, so a file edited after the crash keeps its newer bytes.
    """
    journal.parent.mkdir(parents=True, exist_ok=True)
    files = {p: None if d is None else base64.b64encode(d).decode("ascii") for p, d in saved.items()}
    body: dict[str, Any] = {"version": 2 if placed is not None else 1, "files": files}
    if placed is not None:
        body["placed"] = {p: _digest(d) for p, d in placed.items()}
        body["phase"] = phase
    tmp = journal.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, journal)


def place(files: Mapping[str, Optional[bytes]]) -> None:
    """Write each path's bytes (None = the file must not exist).

    Every path is attempted even when one fails (a test left a directory where
    a file was), so one obstacle never leaves the rest at start bytes;
    ``verify_placed`` reports the outcome.
    """
    for path, data in files.items():
        target = Path(path)
        try:
            if data is None:
                if target.is_file() or target.is_symlink():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        except OSError:
            continue


def verify_placed(files: Mapping[str, Optional[bytes]]) -> bool:
    return all(
        not Path(p).exists() if d is None else Path(p).is_file() and Path(p).read_bytes() == d
        for p, d in files.items()
    )


def _holds(path: str, digest: Optional[str]) -> Optional[bool]:
    """Whether the path holds exactly these bytes; None when it cannot be read."""
    target = Path(path)
    try:
        if digest is None:
            return not target.exists()
        return target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
    except OSError:
        return None


def recover_swap(journal: Path) -> bool:
    """Restore a working tree a crashed baseline run left at start bytes.

    After a crash in the running phase, a path that no longer holds the start
    bytes the swap placed was changed later and keeps its newer bytes. A crash
    while placing or restoring restores every path. A path that cannot be read
    keeps the journal, so no saved byte is ever dropped.
    """
    if not journal.is_file():
        return False
    body = json.loads(journal.read_text(encoding="utf-8"))
    raw = dict(body.get("files") or {})
    files = {p: None if d is None else base64.b64decode(d) for p, d in raw.items()}
    placed = body.get("placed")
    if isinstance(placed, dict) and body.get("phase") == PHASE_RUNNING:
        kept: dict[str, Optional[bytes]] = {}
        for path, data in files.items():
            holds = _holds(path, placed[path]) if path in placed else True
            if holds is None:
                raise RuntimeError(f"KaroX could not read {path}; the journal {journal} keeps its bytes")
            if holds:
                kept[path] = data
        files = kept
    place(files)
    if not verify_placed(files):
        raise RuntimeError(f"KaroX could not restore files from {journal}")
    journal.unlink()
    return True


def recover_pending(repository: Path, wait_seconds: float = 0.0) -> bool:
    """Put back the bytes a process killed mid-swap left at start bytes.

    Called when Core starts and before every check: the swap's own
    ``finally`` never runs for a killed process (codex review, 2026-11-15).
    While another process holds the swap, a check waits up to
    ``wait_seconds`` instead of running on start bytes.
    """
    journal = swap_journal_path(repository)
    lock = journal.with_suffix(".lock")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while journal.is_file() or lock.exists():
        with swap_lock(journal) as held:
            if held:
                return recover_swap(journal)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return False


@contextlib.contextmanager
def swap_lock(journal: Path) -> Iterator[bool]:
    """One baseline swap per repository at a time; yields False when another holds it.

    The holder writes its PID. A lock is stale when its owner is gone or it is
    older than any swap can take; a lock with no PID yet is being created and
    counts as held while it is young.
    """
    from .process_identity import process_is_running

    lock = journal.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                owner = int(lock.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                age, owner = 0.0, 0
            if age < _LOCK_STALE_SECONDS and (owner == 0 or process_is_running(owner)):
                break
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        try:
            yield True
        finally:
            lock.unlink(missing_ok=True)
        return
    yield False
