"""No-secret watchdog for detached Ellipsis-backed KaroX sessions.

The process receives only local identifiers, PIDs and an expiry timestamp. It
never receives the Ellipsis API token, public tunnel URL or bearer credential.
On expiry, or when the local bridge dies, it closes the remaining local process
tree and revokes every local capability that could still reach the repository.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Optional, Sequence

from .ellipsis_runtime import EllipsisAgentState, EllipsisStateStore
from .paths import session_dir
from .remote_lease import EllipsisConnectionStore, EllipsisLeaseStore
from .remote_tools import _kill_pid_tree, _pid_alive
from .sessions import SessionStore


def _cleanup(
    state: EllipsisAgentState,
    *,
    status: str,
    states: EllipsisStateStore,
    leases: EllipsisLeaseStore,
    connections: EllipsisConnectionStore,
    sessions: SessionStore,
) -> None:
    for pid in (state.bridge_pid, state.tunnel_pid):
        if isinstance(pid, int) and pid > 0 and pid != os.getpid() and _pid_alive(pid):
            _kill_pid_tree(pid)
    try:
        leases.revoke(state.credential_name)
    except Exception:
        pass
    connections.delete(state.local_session_id)
    try:
        sessions.revoke(state.local_session_id)
    except Exception:
        pass
    state.status = status
    state.detached = False
    state.bridge_pid = None
    state.tunnel_pid = None
    state.watchdog_pid = None
    states.save(state)


def run_watchdog(
    local_session_id: str,
    *,
    expires_at: float,
    poll_seconds: float = 2.0,
    states: Optional[EllipsisStateStore] = None,
    leases: Optional[EllipsisLeaseStore] = None,
    connections: Optional[EllipsisConnectionStore] = None,
    sessions: Optional[SessionStore] = None,
) -> int:
    state_store = states or EllipsisStateStore()
    lease_store = leases or EllipsisLeaseStore()
    connection_store = connections or EllipsisConnectionStore()
    session_store = sessions or SessionStore(session_dir())
    while True:
        try:
            state = state_store.load(local_session_id)
        except Exception:
            return 0
        if state.status in {"stopped", "failed", "expired"}:
            return 0
        now = time.time()
        if now >= expires_at:
            _cleanup(
                state,
                status="expired",
                states=state_store,
                leases=lease_store,
                connections=connection_store,
                sessions=session_store,
            )
            return 0
        if not isinstance(state.bridge_pid, int) or not _pid_alive(state.bridge_pid):
            _cleanup(
                state,
                status="failed",
                states=state_store,
                leases=lease_store,
                connections=connection_store,
                sessions=session_store,
            )
            return 1
        time.sleep(max(0.2, min(float(poll_seconds), 60.0)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox-ellipsis-watchdog")
    parser.add_argument("--local-session-id", required=True)
    parser.add_argument("--expires-at", required=True, type=float)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    return run_watchdog(
        args.local_session_id,
        expires_at=args.expires_at,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
