"""The Git revision a session started from, read by the trusted runtime.

The completion gate has to know which tree a run began with, not which commit
happens to be ``HEAD`` when the model asks for the closing review: an elevated
run may commit its own work inside the session, and a ``HEAD``-relative
``git diff`` then reports an empty tracked change. A review taken against that
moving HEAD can look complete while never showing the commit the run just
created. The value below is read once, when the session is created, by KaroX
itself; nothing the model says can supply it or move it afterwards.

The persisted record is small and self-describing::

    {"version": 1, "kind": "commit", "commit": "<exact object id>"}
    {"version": 1, "kind": "unborn"}
    {"version": 1, "kind": "unavailable", "reason": "<closed vocabulary>"}

``unborn`` means Git answered that the repository exists and has no commit yet.
``unavailable`` means Git could not answer at all -- not a repository, no Git on
``PATH``, a timeout. The two are kept apart on purpose: a failure must never be
read as "there was nothing to review", and an unborn repository must never be
read as "the start revision is whatever HEAD is now".
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from .security import child_process_environment

#: The shape of the persisted record. A record from a future version is not
#: guessed at; it is "no baseline", which fails closed at the gate.
BASELINE_VERSION = 1

#: Git answered with an exact commit object name.
KIND_COMMIT = "commit"
#: Git answered that the repository exists and has no commit yet.
KIND_UNBORN = "unborn"
#: Git could not answer: no repository, no Git, a timeout, an I/O error.
KIND_UNAVAILABLE = "unavailable"

#: What a complete change review names as the base it actually compared
#: against, and the version of that trusted metadata block. Mission 03 wrote
#: version 1 with a symbolic base (``HEAD`` or ``index``); that shape cannot
#: prove which tree it read, so it is a different contract.
REVIEW_VERSION = 2
REVIEW_BASE_START = "start"
REVIEW_BASE_UNBORN = "unborn"

#: The base is always an exact object name, never a ref or a short name that
#: can move under the review. Both widths Git can produce are accepted (a
#: SHA-256 repository has 64 hex digits); nothing else is.
_OBJECT_NAME = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: A read this slow means the repository is on a broken mount; the session
#: still has to be created, with an explicit "unavailable" baseline.
_CAPTURE_TIMEOUT_SECONDS = 15.0

#: Why Git could not answer. A closed vocabulary, so a session record never
#: carries raw Git stderr.
REASON_NOT_A_REPOSITORY = "not-a-repository"
REASON_GIT_UNAVAILABLE = "git-unavailable"


def _git(repository: Path, *arguments: str) -> Dict[str, Any]:
    """Run one read-only Git read inside ``repository``.

    ``ran`` is False when the process could not start or hit the bounded
    timeout, which is a different fact from an exit code Git chose itself.
    The environment is the same minimal, secret-filtered one Core uses for
    every child process; a hosted run must not forward arbitrary host secrets
    into a Git invocation.
    """

    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(repository),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_CAPTURE_TIMEOUT_SECONDS,
            env=child_process_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ran": False, "exit_code": None, "stdout": ""}
    return {
        "ran": True,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace").strip(),
    }


def capture(repository: Path) -> Dict[str, Any]:
    """Read the revision the repository is at right now.

    One subprocess in the ordinary case (a repository with a commit); a second
    one only when ``HEAD`` does not resolve, to tell "no commit yet" from "not a
    repository". Both are facts about the machine, never about the task.
    """

    captured_at = time.time()
    head = _git(repository, "rev-parse", "--verify", "HEAD")
    if head["exit_code"] == 0 and _OBJECT_NAME.match(str(head["stdout"])):
        return {
            "version": BASELINE_VERSION,
            "kind": KIND_COMMIT,
            "commit": str(head["stdout"]),
            "captured_at": captured_at,
        }
    if not head["ran"]:
        return {
            "version": BASELINE_VERSION,
            "kind": KIND_UNAVAILABLE,
            "reason": REASON_GIT_UNAVAILABLE,
            "captured_at": captured_at,
        }
    inside = _git(repository, "rev-parse", "--git-dir")
    if inside["exit_code"] == 0 and inside["stdout"]:
        return {
            "version": BASELINE_VERSION,
            "kind": KIND_UNBORN,
            "captured_at": captured_at,
        }
    return {
        "version": BASELINE_VERSION,
        "kind": KIND_UNAVAILABLE,
        "reason": REASON_NOT_A_REPOSITORY,
        "captured_at": captured_at,
    }


def normalize(value: Any) -> Optional[Dict[str, Any]]:
    """Return the usable start revision, or ``None`` when it cannot be trusted.

    A record written before this field existed has none; a corrupt, truncated
    or future one is not coerced into shape. Both are "no baseline", and the
    completion gate treats that as "coverage cannot be proved" rather than
    inventing a revision from the current HEAD.
    """

    if not isinstance(value, Mapping):
        return None
    version = value.get("version")
    if isinstance(version, bool) or version != BASELINE_VERSION:
        return None
    kind = value.get("kind")
    if kind == KIND_COMMIT:
        commit = value.get("commit")
        if isinstance(commit, str) and _OBJECT_NAME.match(commit):
            return {
                "version": BASELINE_VERSION,
                "kind": KIND_COMMIT,
                "commit": commit,
            }
        return None
    if kind == KIND_UNBORN:
        return {"version": BASELINE_VERSION, "kind": KIND_UNBORN}
    if kind == KIND_UNAVAILABLE:
        return {"version": BASELINE_VERSION, "kind": KIND_UNAVAILABLE}
    return None


#: The baseline of the session whose command is being executed. Core sets this
#: from the durable record it already loaded, so the Git tools can scope a
#: review without the model -- or a shared runtime serving another session --
#: being able to substitute one.
_CURRENT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "karox_session_git_baseline", default=None
)


@contextlib.contextmanager
def session_baseline(record: Any) -> Iterator[None]:
    """Expose one session's durable start revision while its command runs."""

    token = _CURRENT.set(normalize(getattr(record, "git_baseline", None)))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current() -> Optional[Dict[str, Any]]:
    """The start revision of the session whose command is running, if any."""

    return _CURRENT.get()
