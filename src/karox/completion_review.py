"""Whole-change review and run-identity helpers used by the completion floor."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from . import git_baseline

from .completion_rules import _deliverable, diagnostic_paths


def same_run_inputs(core: Any, prior: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether two identities list the same known inputs.

    ``prior`` is the identity an earlier run was bound to. Every file it hashed
    must hash the same now (re-read from disk when the current identity no
    longer lists it) and the task must be the same. The current identity may
    list more files: files merely read since do not change a byte a command
    executes. The requirement ledger does not affect what a command executes,
    so a ledger edit alone does not count either.

    Equality here is **not** a licence to answer a later call from an earlier
    verdict. The identity names only the files KaroX knows about, and a run
    executes project code that can write a fixture, a lock file or an ignored
    dependency outside that set, so two equal identities do not prove that
    nothing changed between the two runs. Mission 02 removed the verdict cache
    for that reason (``docs/audits/DSH_REUSE_INTEGRITY_20261106.md``); this
    predicate remains the identity comparison itself, and no caller may treat
    it as proof of no side effects.
    """

    if not prior or not current:
        return False
    if prior.get("task") != current.get("task"):
        return False
    prior_files = prior.get("files")
    current_files = current.get("files")
    if not isinstance(prior_files, dict) or not isinstance(current_files, dict):
        return False
    for relative, digest in prior_files.items():
        now = current_files.get(relative)
        if now is None:
            try:
                path = core.safe_path(relative)
                now = (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.is_file()
                    else "missing"
                )
            except Exception:  # noqa: BLE001 - unreadable means changed
                return False
        if now != digest:
            return False
    return True

def command_classes(argv: Sequence[str]) -> set[str]:
    """Exact runner positions, never a substring of arbitrary python -c text."""
    parts = list(argv)
    if not parts:
        return set()
    exe = Path(parts[0]).stem.lower()
    if exe == "git" and parts[1:] == ["diff", "--check"]:
        return {"structural"}
    # The trusted checker is launched with Python -I to prevent task-local
    # shadow modules. Treat exactly that isolated module form as equivalent to
    # python -m; never classify arbitrary python -c stdout as browser proof.
    module = None
    if exe.startswith("python") and len(parts) > 2:
        if parts[1] == "-m":
            module = parts[2]
        elif len(parts) > 3 and parts[1:3] == ["-I", "-m"]:
            module = parts[3]
    if module is not None:
        if module in {"pytest", "unittest"}:
            return {"tests"}
        if module in {"ruff", "mypy", "py_compile", "compileall", "build"}:
            return {"structural"}
        if module == "karox.web_check":
            return {"structural", "browser"} if "--render" in parts else {"structural"}
    if "--help" in parts and "-c" not in parts:
        return {"runtime"}
    if exe in {"pytest"}:
        return {"tests"}
    if exe in {"ruff", "mypy", "tsc"} or (exe == "node" and "--check" in parts):
        return {"structural"}
    if exe in {"npm", "pnpm", "yarn"}:
        script = parts[2] if len(parts) > 2 and parts[1] == "run" else parts[1] if len(parts) > 1 else ""
        if script in {"test:smoke", "test:e2e"}:
            return {"tests", "runtime"}
        if script == "test":
            return {"tests"}
        if script in {"build", "lint", "typecheck", "type-check", "check:types"}:
            return {"structural"}
    if exe in {"cargo", "go"} and len(parts) > 1:
        if parts[1] == "test":
            return {"tests"}
        if parts[1] in {"build", "check", "vet"}:
            return {"structural"}
    return set()

def browser_proof(data: dict[str, Any]) -> bool:
    # An exact same-bytes/same-argv PASS may be replayed from persisted evidence
    # without rerunning the browser process. The cache writer sets this bit only
    # after the original stdout itself passed this function, so the compact replay
    # keeps proof semantics without copying a large render payload into history.
    if (
        data.get("proof_cache_hit") is True
        and data.get("cached_browser_proof") is True
    ):
        return True
    try:
        payload = json.loads(data.get("stdout", ""))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("vacuous"):
        return False
    renders = payload.get("render")
    if not isinstance(renders, dict) or not renders:
        return False
    return all(
        isinstance(r, dict) and not any(r.get(k) for k in ("skipped", "error", "page_errors", "console_errors", "problems"))
        and r.get("sampled", 0) > 0
        for r in renders.values()
    )

def _complete_change_review(record: Any, data: dict[str, Any]) -> bool:
    """Whether this ``git.diff`` read the whole change since the start revision.

    Only the scope the trusted Core actually executed counts. A plain
    ``git diff`` shows the unstaged half only, ``staged=true`` the index half
    only, and ``paths=[...]`` one corner of either; none of them covers the
    change a run is about to hand over, and none of them is taken against the
    tree the run began with.

    The base is checked, not assumed. A review is current only when the exact
    revision it names is the session's durable start revision: a review of
    "whatever HEAD is now" describes a different span after an elevated commit
    moved HEAD, and the model's own claim about the scope is never read --
    ``review`` is written by Core from the commands it ran. A result from
    before this contract (an earlier version, a symbolic base) or from a
    session whose record has no usable start revision fails closed, and so does
    one whose captured tracked diff or untracked listing was truncated: neither
    can prove what it did not show, and a path Git excludes is in neither view.
    """

    review = data.get("review")
    if not isinstance(review, dict):
        return False
    if review.get("version") != git_baseline.REVIEW_VERSION:
        return False
    if review.get("mode") != "complete":
        return False
    if review.get("paths") != []:
        return False
    if review.get("truncated") is not False:
        return False
    if review.get("complete") is not True:
        return False
    ignored = review.get("ignored")  # older shapes cannot prove hidden coverage
    if (
        not isinstance(ignored, dict)
        or ignored.get("complete") is not True
        or ignored.get("excluded")
        or ignored.get("missing")
    ):
        return False
    baseline = git_baseline.normalize(getattr(record, "git_baseline", None))
    if baseline is None:
        return False
    if baseline["kind"] == git_baseline.KIND_COMMIT:
        return (
            review.get("base") == git_baseline.REVIEW_BASE_START
            and review.get("base_commit") == baseline["commit"]
        )
    if baseline["kind"] == git_baseline.KIND_UNBORN:
        return review.get("base") == git_baseline.REVIEW_BASE_UNBORN
    return False

def _review_skips_a_change(core: Any, record: Any, data: dict[str, Any]) -> bool:
    """Whether a complete review explicitly left out a file this run changed.

    An untracked path inside a dependency or build directory is not source
    material and the file tools do not list it, so the review records it as
    excluded rather than pretending to show it. When the run itself wrote such
    a file, that is a real gap: the gate must keep asking instead of counting
    a review that skipped the deliverable.
    """

    review = data.get("review")
    untracked = review.get("untracked") if isinstance(review, dict) else None
    excluded = untracked.get("excluded") if isinstance(untracked, dict) else None
    if not isinstance(excluded, list) or not excluded:
        return False
    skipped = {path for path in excluded if isinstance(path, str)}
    diagnostic = diagnostic_paths(core, record)
    return any(
        path in skipped and _deliverable(path, diagnostic)
        for path in record.changed_files
    )
