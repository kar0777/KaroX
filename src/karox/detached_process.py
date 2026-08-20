"""Start a long-lived child that outlives whatever started it.

Every durable KaroX process -- a saved bridge's owner, its supervisor -- has the
same requirement: it must keep running after the thing that launched it goes
away. On Windows that means escaping the launcher's **job object**, because a
terminal, an agent session or an IDE typically runs inside one and Windows kills
every process in a job when the job is torn down.

``CREATE_BREAKAWAY_FROM_JOB`` is the flag for that, and it is tried first. It is
not always available: a caller whose own job was created without
``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` cannot use it, and ``CreateProcess`` then fails
outright with ``WinError 5`` -- it does not quietly ignore the flag. Task
Scheduler runs every action inside exactly such a job, which is why the OS
revival layer could not start anything at all until this fell back: measured on
Windows 11, the 5-minute watchdog raised ``PermissionError`` on its only attempt
and reported nothing but ``Last Result: 1``.

So breakaway is an attempt, not a requirement. When it is refused the child is
started inside the caller's job instead, which is the correct owner in that
situation anyway: the Task Scheduler service outlives every terminal, every
logoff and every reboot, and it is the one launcher KaroX actually wants to
inherit from. The mechanism that worked is returned so callers can record it --
an unattended start that fails silently is the one failure mode this module
exists to prevent.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CREATE_BREAKAWAY_FROM_JOB = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)


def detached_flags(*, breakaway: bool = True) -> int:
    """Creation flags for a windowless child with no console of its own."""
    if os.name != "nt":
        return 0
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    return flags | CREATE_BREAKAWAY_FROM_JOB if breakaway else flags


def spawn_detached(
    argv: list[str],
    *,
    cwd: Optional[str] = None,
) -> tuple[Optional[Any], str]:
    """Start ``argv`` detached, leaving the caller's job object when allowed.

    Returns ``(process, mechanism)``. ``process`` is ``None`` only when no attempt
    succeeded, and ``mechanism`` then begins with ``spawn_failed:`` and names the
    Windows error of each attempt. On success ``mechanism`` is ``breakaway`` or
    ``in_caller_job``, with any failed attempt appended so a state file or a log
    line records what the environment actually permitted.
    """
    attempts: list[tuple[str, int]] = [("breakaway", detached_flags())]
    fallback = detached_flags(breakaway=False)
    if fallback != attempts[0][1]:
        attempts.append(("in_caller_job", fallback))
    failures: list[str] = []
    for name, flags in attempts:
        try:
            process = subprocess.Popen(  # noqa: S603 - callers pass fixed argv
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            failures.append(f"{name}:{getattr(exc, 'winerror', None) or type(exc).__name__}")
            continue
        if failures:
            return process, f"{name} after {','.join(failures)}"
        return process, name
    return None, f"spawn_failed:{','.join(failures)}"


__all__ = [
    "CREATE_BREAKAWAY_FROM_JOB",
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "DETACHED_PROCESS",
    "detached_flags",
    "spawn_detached",
]
