"""Pre-existing pytest failures: rerun a failed check on the session's start bytes
(HARD12 2026-11-10: a red-before-edit suite made the model "repair" unrelated code).
Fail-closed: same argv/exit code, no timeout, >0 and >= passed, no new failing id."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Parameters may hold " - " ("t[a - b]"): the node keeps its whole bracket.
_SUMMARY_ROW = re.compile(r"^(?:FAILED|ERROR) (?P<node>[^\s\[]+(?:\[[^\]]*\])?\S*)(?: - .*)?$", re.M)
_COUNT = re.compile(r"(?P<n>\d+) (?P<kind>passed|failed|errors?)\b")
_FINAL = re.compile(r"^=*\s*\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b.*\bin [\d.]+s", re.M)
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


def is_pytest(argv: Sequence[str]) -> bool:
    parts = [str(p) for p in argv]
    exe = Path(parts[0]).stem.lower() if parts else ""
    if exe in {"pytest", "py.test"}:
        return True
    return exe.startswith("python") and parts[1:3] == ["-m", "pytest"]


def missing_tool(argv: Sequence[str], result: Mapping[str, Any]) -> str:
    """The tool a failed check could not even start (`No module named ruff`, exit 127): no evidence either way."""
    parts, text = [str(p) for p in argv], f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
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
    """Every NEW failure versus the start bytes is a declared superseded test (pytest only)."""
    new = (data.get("baseline") or {}).get("new") if isinstance(data.get("baseline"), Mapping) else None
    return bool(names and new) and all(any(n == d or n.endswith("::" + d) for d in names) for n in new)


def outcome(text: str, exit_code: Any, timed_out: Any) -> Outcome:
    nodes = (m.group("node").strip() for m in _SUMMARY_ROW.finditer(text or ""))
    finals = list(_FINAL.finditer(text or ""))
    passed = sum(
        int(m.group("n")) for m in _COUNT.finditer(finals[-1].group(0)) if m.group("kind") == "passed"
    ) if finals else 0
    code = int(exit_code) if isinstance(exit_code, int) or str(exit_code).lstrip("-").isdigit() else -1
    failing = frozenset(n for n in nodes if "::" in n or ".py" in n)
    return Outcome(code, bool(timed_out), failing, passed, bool(finals))


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
        and current.exit_code == baseline.exit_code
        and 0 < current.passed
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


def write_swap_journal(journal: Path, saved: Mapping[str, Optional[bytes]]) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    files = {p: None if d is None else base64.b64encode(d).decode("ascii") for p, d in saved.items()}
    tmp = journal.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "files": files}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, journal)


def place(files: Mapping[str, Optional[bytes]]) -> None:
    """Write each path's bytes (None = the file must not exist)."""
    for path, data in files.items():
        target = Path(path)
        if data is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def verify_placed(files: Mapping[str, Optional[bytes]]) -> bool:
    return all(
        not Path(p).exists() if d is None else Path(p).is_file() and Path(p).read_bytes() == d
        for p, d in files.items()
    )


def recover_swap(journal: Path) -> bool:
    """Restore a working tree a crashed baseline run left at start bytes."""
    if not journal.is_file():
        return False
    raw = dict(json.loads(journal.read_text(encoding="utf-8")).get("files") or {})
    files = {p: None if d is None else base64.b64decode(d) for p, d in raw.items()}
    place(files)
    if not verify_placed(files):
        raise RuntimeError(f"KaroX could not restore files from {journal}")
    journal.unlink()
    return True
