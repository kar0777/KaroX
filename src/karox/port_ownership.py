"""Port and process ownership for saved bridge profiles.

Phase 0.3 of the recovery plan requires that restarting a saved bridge never
depends on "kill whatever holds the port". Instead the launcher -- and the new
``bridge status/stop/restart/attach --saved NAME`` commands -- need a single
verdict that classifies who owns the port right now:

* a live bridge of the *same* profile (reuse, do not relaunch);
* a live bridge of the *same* profile whose config differs (controlled restart);
* a stale KaroX process whose PID may have been reused (prove ownership first);
* this profile's own ``bridge serve`` child that outlived its owner (reclaimable
  once the process proves its identity, never before);
* an unrelated process (leave it alone, pick another port or error honestly);
* nothing (free to launch).

This module reads the watchdog records the launcher already writes, checks the
recorded PID against the live system (with creation-time verification so a
reused PID is never mistaken for the original), and reports the verdict with
the full ownership metadata the brief lists -- profile, session, PID, start
time, executable, repository, port, URL, tunnel -- but never a secret.
"""

from __future__ import annotations

import dataclasses
import json
import ntpath
import re
import socket
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .process_identity import ProcessIdentity, argv_digest, read_process_create_time_ns
from .web_bridge_launcher import (
    _process_is_alive,
    saved_web_bridge_session_candidates,
    watchdog_dir,
)


# A watchdog record is replaced atomically on every heartbeat; a reader that
# collides with that rename must wait it out rather than conclude "no bridge".
_RECORD_READ_TIMEOUT_SECONDS = 2.0
_RECORD_READ_INITIAL_DELAY_SECONDS = 0.02
_RECORD_READ_MAX_DELAY_SECONDS = 0.2


# --------------------------------------------------------------------------- #
# Verdict                                                                     #
# --------------------------------------------------------------------------- #


# Public verdict codes. A string, not an enum, so a JSON payload and a human
# message can both carry the same word without an import.
OWNERSHIP_FREE = "free"
OWNERSHIP_REUSE_SAME = "reuse_same_profile"
OWNERSHIP_RESTART_CONFIG_DIFFERS = "config_differs_same_profile"
OWNERSHIP_STALE_OWNED = "stale_owned_process"
OWNERSHIP_UNRELATED = "unrelated_process"
OWNERSHIP_PORT_UNAVAILABLE = "port_unavailable_unknown_owner"


@dataclasses.dataclass(frozen=True)
class OwnershipMetadata:
    """The full ownership record for a saved profile's port, without secrets."""

    profile: Optional[str]
    credential_reference: Optional[str]
    session_id: Optional[str]
    pid: Optional[int]
    process_start_time_ns: Optional[int]
    executable_path: Optional[str]
    repository: Optional[str]
    local_host: Optional[str]
    local_port: Optional[int]
    public_url: Optional[str]
    tunnel_type: Optional[str]
    route_identity: Optional[str]
    config_digest: Optional[str]
    creation_timestamp: Optional[float]
    watchdog_path: Optional[str]
    # Is the recorded PID provably the one the launcher started? False when the
    # PID died or was reused, which is exactly the case where stopping by PID
    # would be unsafe.
    pid_proven: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class OwnershipVerdict:
    """The one-word classification plus the evidence behind it."""

    verdict: str
    reason: str
    metadata: OwnershipMetadata
    # When an unrelated process holds the port we can see *its* PID (via the OS
    # port table) but we must never name it as ours.
    unrelated_pid: Optional[int] = None
    # When the port is held by a process that *proves* it is this profile's own
    # ``bridge serve`` child -- an owner died and left its listener behind --
    # this is that PID. Recovery may reclaim it; nothing else may be touched.
    owned_orphan_pid: Optional[int] = None
    # Set when that listener's parent proves, from its own argv, that it is a
    # live ``bridge connect --saved <profile>`` owner whose watchdog record is
    # missing. Reclaiming only the child cannot work in that state: the live
    # owner respawns it. Orphan recovery must leave this live owner alone.
    live_unrecorded_owner_pid: Optional[int] = None
    # Snapshot of the listener instance, not the dead owner in metadata. A PID
    # without this evidence is diagnostic only and cannot authorize recovery.
    owned_orphan_identity: Optional[ProcessIdentity] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "verdict": self.verdict,
            "reason": self.reason,
            "metadata": self.metadata.to_dict(),
        }
        if self.unrelated_pid is not None:
            result["unrelated_pid"] = self.unrelated_pid
        if self.owned_orphan_pid is not None:
            result["owned_orphan_pid"] = self.owned_orphan_pid
        if self.live_unrecorded_owner_pid is not None:
            result["live_unrecorded_owner_pid"] = self.live_unrecorded_owner_pid
        if self.owned_orphan_identity is not None:
            result["owned_orphan_identity"] = self.owned_orphan_identity.to_dict()
        return result


# --------------------------------------------------------------------------- #
# Reading watchdog records                                                    #
# --------------------------------------------------------------------------- #


def _read_watchdog_text(path: Path) -> str:
    """Read the raw record text. Separated so the retry policy is testable."""
    return path.read_text(encoding="utf-8")


def _load_watchdog_record(path: Path) -> Optional[dict[str, Any]]:
    """Read one watchdog JSON record, returning ``None`` on any corruption.

    A malformed record is not ownership evidence: the caller must fail-safe
    (report ``stale`` or ``unavailable``) rather than act on it.

    A *transient* read error is different. ``write_watchdog`` replaces the record
    on every heartbeat, and on Windows an opener can lose that race with a
    sharing violation. The writer already retries for the mirror-image reason;
    without the same patience here a healthy bridge is misread as "no owned
    bridge", which downgrades it to ``unrelated_process`` and blocks restart.
    """
    deadline = time.monotonic() + _RECORD_READ_TIMEOUT_SECONDS
    delay = _RECORD_READ_INITIAL_DELAY_SECONDS
    while True:
        try:
            raw = _read_watchdog_text(path)
        except FileNotFoundError:
            # Not contention: there is genuinely no record for this session.
            return None
        except OSError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(delay)
            delay = min(delay * 2.0, _RECORD_READ_MAX_DELAY_SECONDS)
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        return record


def _find_active_watchdog(profile_name: str) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
    """Return the watchdog record for ``profile_name`` whose owner is alive.

    The saved-profile identity scheme produces a small set of candidate session
    IDs (current + legacy). The first candidate with an existing watchdog whose
    recorded owner PID is still alive is the active bridge for this profile.
    """
    root = watchdog_dir()
    for session_id in saved_web_bridge_session_candidates(profile_name):
        path = root / f"{session_id}.json"
        record = _load_watchdog_record(path)
        if record is None:
            continue
        owner_pid = record.get("owner_pid")
        if isinstance(owner_pid, int) and _process_is_alive(owner_pid):
            return path, record
    return None, None


def _build_metadata(record: dict[str, Any], path: Path) -> OwnershipMetadata:
    """Build the safe metadata block from a watchdog record.

    No secret is read here: the record never contains one, and
    ``credential_reference`` is the opaque ``os-keyring:bridge/...`` string.
    """
    owner_pid = record.get("owner_pid")
    pid_int = owner_pid if isinstance(owner_pid, int) and owner_pid > 0 else None
    start_time: Optional[int] = None
    pid_proven = False
    if pid_int is not None:
        # Read the live creation time to prove the PID is still the process the
        # launcher started. A mismatch means the PID was reused and must never
        # be terminated as though it were ours.
        observed = read_process_create_time_ns(pid_int)
        if observed is not None:
            start_time = observed
            pid_proven = True
    session_id = record.get("session_id")
    credential_reference: Optional[str] = None
    if isinstance(session_id, str) and session_id:
        credential_reference = f"os-keyring:bridge/{session_id}"
    return OwnershipMetadata(
        profile=record.get("saved_profile") or record.get("profile"),
        credential_reference=credential_reference,
        session_id=session_id if isinstance(session_id, str) else None,
        pid=pid_int,
        process_start_time_ns=start_time,
        executable_path=None,  # not recorded in the watchdog; populate later if added
        repository=None,
        local_host="127.0.0.1",
        local_port=record.get("port") if isinstance(record.get("port"), int) else None,
        public_url=record.get("public_url") if isinstance(record.get("public_url"), str) else None,
        tunnel_type=record.get("tunnel") if isinstance(record.get("tunnel"), str) else None,
        route_identity=None,
        config_digest=None,
        creation_timestamp=record.get("started_at") if isinstance(record.get("started_at"), (int, float)) else None,
        watchdog_path=str(path),
        pid_proven=pid_proven,
    )


def _empty_metadata() -> OwnershipMetadata:
    return OwnershipMetadata(
        profile=None,
        credential_reference=None,
        session_id=None,
        pid=None,
        process_start_time_ns=None,
        executable_path=None,
        repository=None,
        local_host=None,
        local_port=None,
        public_url=None,
        tunnel_type=None,
        route_identity=None,
        config_digest=None,
        creation_timestamp=None,
        watchdog_path=None,
        pid_proven=False,
    )


# --------------------------------------------------------------------------- #
# Port probing                                                                 #
# --------------------------------------------------------------------------- #


def _port_has_listener(port: int, *, host: str = "127.0.0.1") -> bool:
    """True when something is listening on ``port``.

    The connect probe distinguishes "free" from "in use" without binding, so it
    does not evict a transient listener the way a bind probe can on some stacks.
    """
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _port_owning_pid(port: int) -> Optional[int]:
    """Best-effort lookup of the PID listening on ``port``, or ``None``.

    This never authorises termination: it only populates the
    ``unrelated_pid`` diagnostic so the user can investigate. Stopping always
    requires a proven watchdog match, never this value.
    """
    if port <= 0:
        return None
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        owners = {
            connection.pid
            for connection in psutil.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and connection.laddr.port == port
        }
        # Several interfaces/families can bind the same numeric port. Never
        # choose whichever table entry happens to come first as kill evidence.
        if len(owners) == 1:
            return owners.pop()
    except Exception:
        return None
    return None


def _process_command_line(pid: int) -> Optional[list[str]]:
    """Best-effort argv of ``pid``, or ``None`` when it cannot be read.

    ``None`` is the fail-closed answer: without argv there is no proof, and
    without proof the holder must be treated as foreign.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        argv = psutil.Process(pid).cmdline()
    except Exception:
        return None
    if not argv:
        return None
    return [str(token) for token in argv]


def _bridge_command_arguments(argv: list[str], command: str) -> Optional[list[str]]:
    """Recognize a real CLI invocation, never incidental words in an argv.

    Session ids are identifiers, not secrets or authentication. Ownership also
    requires OS-account and creation-time checks at the termination boundary.
    """
    if not argv:
        return None
    executable = ntpath.basename(argv[0]).lower()
    if executable in {"karox", "karox.exe", "karox-vnext", "karox-vnext.exe"}:
        arguments = argv[1:]
    elif re.fullmatch(r"python(?:w|[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", executable):
        # Python flags emitted by launchers must not hide a live parent owner.
        offset = 1
        while offset < len(argv) and argv[offset] in {"-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO", "-q"}:
            offset += 1
        if argv[offset:offset + 2] not in (["-m", "karox.cli"], ["-m", "karox"]):
            return None
        arguments = argv[offset + 2:]
    else:
        return None
    if arguments[:2] != ["bridge", command]:
        return None
    return arguments[2:]


def _unique_option(arguments: list[str], option: str) -> Optional[str]:
    values: list[str] = []
    for index, token in enumerate(arguments):
        if token == option and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif token.startswith(option + "="):
            values.append(token.split("=", 1)[1])
    return values[0] if len(values) == 1 and values[0] else None


def prove_bridge_process_identity(
    pid: int,
    session_ids: Iterable[str],
    *,
    command_line: Optional[list[str]] = None,
) -> Optional[str]:
    """Return the session id ``pid`` proves it serves, or ``None``.

    Proof is the process's own command line: it must be a KaroX ``bridge serve``
    invocation carrying one of this profile's session ids. A session id is a
    profile-derived identifier (not an authentication secret), and
    an incidental mention (``bridge status --saved <id>``) is not accepted --
    only the serving subcommand counts.
    """
    argv = command_line if command_line is not None else _process_command_line(pid)
    if not argv:
        return None
    arguments = _bridge_command_arguments([str(token) for token in argv], "serve")
    if arguments is None:
        return None
    value = _unique_option(arguments, "--session-id")
    return value if value in set(session_ids) else None


def extract_bridge_process_info(
    pid: Optional[int],
    *,
    command_line: Optional[list[str]] = None,
) -> Optional[dict[str, str]]:
    """Extract bridge parameters if `pid` is running a KaroX bridge serve command."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    argv = command_line if command_line is not None else _process_command_line(pid)
    if not argv:
        return None
    tokens = [str(token) for token in argv]
    if "karox" not in " ".join(tokens).lower():
        return None
    if "bridge" not in tokens or "serve" not in tokens:
        return None
    info: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token == "--session-id" and index + 1 < len(tokens):
            info["session_id"] = tokens[index + 1]
        elif token.startswith("--session-id="):
            info["session_id"] = token.split("=", 1)[1]
        elif token == "--profile" and index + 1 < len(tokens):
            info["profile"] = tokens[index + 1]
        elif token.startswith("--profile="):
            info["profile"] = token.split("=", 1)[1]
        elif token == "--public-url" and index + 1 < len(tokens):
            info["public_url"] = tokens[index + 1]
        elif token.startswith("--public-url="):
            info["public_url"] = token.split("=", 1)[1]
    return info if info else None


def prove_saved_bridge_owner_identity(pid: Optional[int], profile_name: str) -> bool:
    """True when ``pid`` proves, from its own argv, that it owns ``profile_name``.

    Proof is a KaroX ``bridge connect --saved <profile_name>`` invocation. That is
    the durable owner process itself, so a match means the owner is *running*
    regardless of whether its watchdog record still exists on disk. Without argv
    there is no proof and the answer is ``False``.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    argv = _process_command_line(pid)
    if not argv:
        return False
    arguments = _bridge_command_arguments([str(token) for token in argv], "connect")
    return arguments is not None and _unique_option(arguments, "--saved") == profile_name


def _live_owner_of_listener(pid: int, profile_name: str) -> Optional[int]:
    """Return the PID of a live owner that is the parent of ``pid``, if any.

    An owner whose watchdog record is missing is invisible to
    ``_find_active_watchdog``, so its still-serving child looks orphaned. Killing
    only that child is futile: the live owner immediately respawns one and the
    port never becomes free. Naming the owner prevents orphan recovery from
    recycling a live bridge merely because its watchdog record disappeared.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        parent = psutil.Process(pid).parent()
    except Exception:
        return None
    seen: set[int] = set()
    while parent is not None:
        parent_pid = int(parent.pid)
        if parent_pid <= 0 or parent_pid in seen:
            return None
        seen.add(parent_pid)
        if prove_saved_bridge_owner_identity(parent_pid, profile_name):
            return parent_pid
        try:
            parent = parent.parent()
        except Exception:
            return None
    return None


def _owned_orphan_listener(
    profile_name: str, holder_pid: Optional[int]
) -> tuple[Optional[int], Optional[str]]:
    """Classify the port holder when no live owner is recorded.

    Returns ``(pid, session_id)`` when the holder proves it is this profile's own
    ``bridge serve`` child that outlived its owner, else ``(None, None)``.
    """
    if not isinstance(holder_pid, int) or holder_pid <= 0:
        return None, None
    candidates = tuple(saved_web_bridge_session_candidates(profile_name))
    session_id = prove_bridge_process_identity(holder_pid, candidates)
    if session_id is None:
        return None, None
    return holder_pid, session_id


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #


def check_port_ownership(profile_name: str, *, port: int) -> OwnershipVerdict:
    """Classify the ownership of ``port`` for the saved profile ``profile_name``.

    The function is read-only and side-effect free: it never terminates a
    process, never deletes a watchdog record, and never reads a secret. The
    caller decides what to do with the verdict.

    Cases (matching the brief's A-E):

    * port free and no owned bridge -> ``free``
    * owned bridge alive, same profile -> ``reuse_same_profile``
    * owned bridge alive but stale (PID reused / create-time mismatch) ->
      ``stale_owned_process``
    * the owner is gone but its own ``bridge serve`` child still holds the port
      -> ``stale_owned_process`` with ``owned_orphan_pid`` set
    * something listening but no owned bridge matches -> ``unrelated_process``
    """
    path, record = _find_active_watchdog(profile_name)
    if record is not None and path is not None:
        metadata = _build_metadata(record, path)
        owner_pid = record.get("owner_pid")
        if isinstance(owner_pid, int):
            # Verify the PID is still the process the launcher started. The
            # _find_active_watchdog check already confirms liveness; this second
            # check adds the creation-time proof that defeats PID reuse.
            observed = read_process_create_time_ns(owner_pid)
            if observed is None:
                # Cannot prove ownership (e.g. permission denied): report stale
                # so the caller asks the user rather than terminating.
                return OwnershipVerdict(
                    OWNERSHIP_STALE_OWNED,
                    f"watchdog owner PID {owner_pid} is alive but its creation "
                    "time cannot be verified; do not terminate without proof",
                    metadata,
                )
            return OwnershipVerdict(
                OWNERSHIP_REUSE_SAME,
                f"port {port} is held by a live bridge for profile "
                f"'{profile_name}' (PID {owner_pid}, proven by creation time)",
                metadata,
            )
    # No owned bridge is alive. Is the port actually free?
    if not _port_has_listener(port):
        return OwnershipVerdict(
            OWNERSHIP_FREE,
            f"port {port} is free and no bridge for '{profile_name}' is recorded",
            _empty_metadata(),
        )
    # Something is listening, but no *owner* of ours is alive. Before calling it
    # foreign, ask the holder to prove it is this profile's own bridge child: an
    # owner can die and leave its local MCP listener running. Calling that child
    # "unrelated" deadlocks recovery on a port KaroX itself occupies.
    unrelated_pid = _port_owning_pid(port)
    orphan_created = (
        read_process_create_time_ns(unrelated_pid) if unrelated_pid is not None else None
    )
    orphan_pid, orphan_session = _owned_orphan_listener(profile_name, unrelated_pid)
    if orphan_pid is not None and orphan_session is not None:
        record_path = watchdog_dir() / f"{orphan_session}.json"
        orphan_record = _load_watchdog_record(record_path)
        metadata = (
            _build_metadata(orphan_record, record_path)
            if orphan_record is not None
            else _empty_metadata()
        )
        orphan_identity = None
        argv = _process_command_line(orphan_pid)
        if (
            orphan_created is not None
            and argv
            and prove_bridge_process_identity(orphan_pid, (orphan_session,), command_line=argv)
            and read_process_create_time_ns(orphan_pid) == orphan_created
        ):
            orphan_identity = ProcessIdentity(
                pid=orphan_pid, create_time_ns=orphan_created, argv_sha256=argv_digest(argv)
            )
        live_owner_pid = _live_owner_of_listener(orphan_pid, profile_name)
        if live_owner_pid is not None:
            return OwnershipVerdict(
                OWNERSHIP_STALE_OWNED,
                f"port {port} is held by this profile's own bridge process "
                f"(PID {orphan_pid}, session {orphan_session}) whose owner "
                f"(PID {live_owner_pid}) is still running without a watchdog "
                "record; orphan recovery must leave the live owner alone",
                metadata,
                owned_orphan_pid=orphan_pid,
                live_unrecorded_owner_pid=live_owner_pid,
                owned_orphan_identity=orphan_identity,
            )
        return OwnershipVerdict(
            OWNERSHIP_STALE_OWNED,
            f"port {port} is held by this profile's own bridge process "
            f"(PID {orphan_pid}, session {orphan_session}) whose owner is gone; "
            "recovery may reclaim it",
            metadata,
            owned_orphan_pid=orphan_pid,
            owned_orphan_identity=orphan_identity,
        )
    # Not provably ours. Check if it is a KaroX bridge for another profile or session:
    foreign_info = extract_bridge_process_info(unrelated_pid)
    if foreign_info and foreign_info.get("session_id"):
        foreign_sess = foreign_info["session_id"]
        foreign_prof = foreign_info.get("profile", "unknown")
        holder_name = foreign_prof
        try:
            from .web_bridge_profiles import WebBridgeProfileStore
            for saved_p in WebBridgeProfileStore().list():
                if foreign_sess in saved_web_bridge_session_candidates(saved_p.name):
                    holder_name = saved_p.name
                    break
        except Exception:
            pass
        reason = (
            f"port {port} is in use by KaroX bridge profile '{holder_name}' "
            f"(PID {unrelated_pid}, session {foreign_sess}); cannot start bridge for '{profile_name}'"
        )
    else:
        reason = (
            f"port {port} is held by a process that is not a proven bridge for "
            f"'{profile_name}'; do not terminate it"
        )
    metadata = _empty_metadata()
    return OwnershipVerdict(
        OWNERSHIP_UNRELATED,
        reason,
        metadata,
        unrelated_pid=unrelated_pid,
    )


__all__ = [
    "OWNERSHIP_FREE",
    "OWNERSHIP_PORT_UNAVAILABLE",
    "OWNERSHIP_RESTART_CONFIG_DIFFERS",
    "OWNERSHIP_REUSE_SAME",
    "OWNERSHIP_STALE_OWNED",
    "OWNERSHIP_UNRELATED",
    "OwnershipMetadata",
    "OwnershipVerdict",
    "check_port_ownership",
    "extract_bridge_process_info",
    "prove_bridge_process_identity",
    "prove_saved_bridge_owner_identity",
]
