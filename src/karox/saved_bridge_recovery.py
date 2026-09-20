"""Fail-closed orphan recovery shared by saved start and supervision.

An ownership verdict is evidence, not a license to kill a PID later. Recheck the
instance, account, argv, port and owner immediately before signalling. Never
recycle a live owner here, even if its watchdog file disappeared. Never use
``taskkill /T`` or kill a process group just to reclaim a listening socket.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Optional

import psutil  # type: ignore[import-untyped]

from .port_ownership import (
    OWNERSHIP_STALE_OWNED,
    OwnershipVerdict,
    _bridge_command_arguments,
    _port_owning_pid,
    _unique_option,
    check_port_ownership,
    prove_bridge_process_identity,
    saved_web_bridge_session_candidates,
)
from .process_identity import (
    ProcessIdentity,
    read_process_create_time_ns,
    verify_process_identity,
)

ORPHAN_RECLAIM_TIMEOUT_SECONDS = 10.0


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _parents_are_unowned(process: Any) -> bool:
    """Only an exited launch parent is evidence of an orphan.

    A lost watchdog or an unrecognized owner argv does not make a live child
    safe to kill. On POSIX genuine orphans are adopted by init; Windows retains
    the exited parent's PID and psutil excludes nonexistent/reused parents.
    A non-system subreaper is conservatively left for explicit owner recovery.
    """
    try:
        return all(parent.pid in {0, 1, 4} for parent in process.parents())
    except psutil.Error:
        return False


def _same_orphan(verdict: OwnershipVerdict, identity: ProcessIdentity) -> bool:
    observed = verdict.owned_orphan_identity
    return (
        verdict.verdict == OWNERSHIP_STALE_OWNED
        and verdict.owned_orphan_pid == identity.pid
        and verdict.live_unrecorded_owner_pid is None
        and observed is not None
        and observed.create_time_ns == identity.create_time_ns
        and observed.argv_sha256 == identity.argv_sha256
    )


def reclaim_saved_bridge_orphan(
    profile_name: str,
    *,
    port: int,
    ownership: Optional[OwnershipVerdict] = None,
) -> bool:
    """Release only this profile's proven orphan listener, or leave it alone.

    ``ownership`` carries the creation-time/argv snapshot taken by the port
    classifier. Missing evidence, account/argv changes, live owners and PID
    reuse all fail closed. A fresh psutil Process retains its own instance
    identity and checks for PID reuse when terminate() is called. Waiting never
    escalates to a PID-only kill; recovery can retry on a later supervisor tick.

    Success requires the listener to have exited *and* the port to be bindable.
    No credentials, routes, saved identities or desired-running state change.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return False
    verdict = ownership if ownership is not None else check_port_ownership(profile_name, port=port)
    identity = getattr(verdict, "owned_orphan_identity", None)
    if (
        identity is None
        or not identity.provable
        or not identity.argv_sha256
        or identity.pid == os.getpid()
        or not _same_orphan(verdict, identity)
    ):
        return False
    try:
        process = psutil.Process(identity.pid)
        # Same-account ownership is required even if a foreign account copied
        # the profile-derived (non-secret) session identifier into its argv.
        account = process.username()
        if account != psutil.Process(os.getpid()).username():
            return False
        executable = process.exe()
        argv = process.cmdline()
        arguments = _bridge_command_arguments(argv, "serve")
        if (
            not executable
            or arguments is None
            or _unique_option(arguments, "--port") != str(port)
            or prove_bridge_process_identity(
                identity.pid, saved_web_bridge_session_candidates(profile_name), command_line=argv
            ) is None
            or not _parents_are_unowned(process)
        ):
            return False
        current = check_port_ownership(profile_name, port=port)
        if not _same_orphan(current, identity):
            return False
        if _port_owning_pid(port) != identity.pid:
            return False
        if (
            process.username() != account
            or process.exe() != executable
            or not _parents_are_unowned(process)
            or not verify_process_identity(
                identity,
                pid_alive=lambda _pid: process.is_running(),
                create_time_reader=read_process_create_time_ns,
                expected_argv=process.cmdline(),
            ).proven
        ):
            return False
        process.terminate()
        process.wait(timeout=ORPHAN_RECLAIM_TIMEOUT_SECONDS)
    except (psutil.Error, OSError, ValueError):
        return False

    deadline = time.monotonic() + ORPHAN_RECLAIM_TIMEOUT_SECONDS
    while True:
        if _port_available(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
