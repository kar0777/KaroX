"""Safe self-restart coordination for a durable saved web-bridge child.

The public MCP child must never terminate itself while its own tool response is
still on the wire. A durable saved bridge already has a stronger owner process
which holds the public route, OAuth/keyring identity and session, and respawns a
local MCP child when that child exits. This module uses that existing recovery
layer instead of restarting the owner/tunnel.

A restart is allowed only when the current process can prove, from the watchdog
record and parent relationship, that it is the exact local child of the requested
saved profile. The operation is idempotent across the child replacement: raw
idempotency keys and user reasons are never persisted, only hashes and secret-free
status metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .bridge_handoff import (
    BridgeHandoffError,
    read_rolling_restart_request,
    request_rolling_restart,
)
from .paths import runtime_dir
from .port_ownership import prove_bridge_process_identity, prove_saved_bridge_owner_identity
from .process_identity import process_is_running

SELF_RESTART_EXIT_CODE = 75
RESTART_IDLE_TIMEOUT_SECONDS = 30.0
RESTART_HANDOFF_TIMEOUT_SECONDS = 20.0
RESTART_POLL_SECONDS = 0.05
RESTART_MIN_GRACE_SECONDS = 0.10
_MAX_REASON_LENGTH = 500
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimeRestartError(RuntimeError):
    """A requested child restart could not be proven safe."""


def _watchdog_path(session_id: str) -> Path:
    return runtime_dir() / "web-bridge" / f"{session_id}.json"


def _receipt_dir(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return runtime_dir() / "runtime-restarts" / digest


def _receipt_path(session_id: str, idempotency_key: str) -> Path:
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return _receipt_dir(session_id) / f"{key_digest}.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _create_receipt_exclusive(path: Path, value: Mapping[str, Any]) -> bool:
    """Create one receipt without overwriting a concurrent/replayed request."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _request_digest(saved_profile: str, reason: str) -> str:
    # User-provided reasons may accidentally contain a secret. Persist only a digest.
    return hashlib.sha256(f"{saved_profile}\0{reason}".encode("utf-8")).hexdigest()


def _claim_timeout_reschedule(receipt_path: Path, attempt: int) -> bool:
    """Atomically claim one re-arm after a previous transport-safe timeout."""
    claim = receipt_path.with_name(f"{receipt_path.name}.retry-{attempt}.lock")
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, b"claimed\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _process_parent_pid(pid: int) -> Optional[int]:
    """Return a live process parent PID when it can be proven, else ``None``.

    The saved bridge may be launched through a Windows venv ``python.exe`` shim.
    In that topology the watchdog records the shim PID while the actual Python
    runtime serving MCP is the shim's direct child.  ``psutil`` is already an
    optional KaroX runtime dependency used by the ownership layer; if it is not
    available we fail closed and keep the historical direct-parent rule.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        parent = int(psutil.Process(pid).ppid())
    except Exception:
        return None
    return parent if parent > 0 else None


def _validate_current_saved_child(session_id: str, saved_profile: str) -> dict[str, Any]:
    if not isinstance(saved_profile, str) or _SAFE_PROFILE.fullmatch(saved_profile) is None:
        raise RuntimeRestartError("runtime restart requires a valid saved profile")
    watchdog = _read_json(_watchdog_path(session_id))
    if not watchdog:
        raise RuntimeRestartError("runtime restart requires a live saved-bridge watchdog")
    if watchdog.get("session_id") != session_id:
        raise RuntimeRestartError("saved-bridge watchdog belongs to another session")
    if watchdog.get("saved_profile") != saved_profile:
        raise RuntimeRestartError("saved-bridge watchdog belongs to another profile")
    if watchdog.get("persistent_session") is not True:
        raise RuntimeRestartError("runtime restart is available only for durable saved bridges")

    bridge_pid = watchdog.get("bridge_pid")
    if not isinstance(bridge_pid, int) or isinstance(bridge_pid, bool) or bridge_pid <= 0:
        raise RuntimeRestartError("saved bridge MCP PID is unavailable")
    owner_pid = watchdog.get("owner_pid")
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0:
        raise RuntimeRestartError("saved bridge owner PID is unavailable")
    if not process_is_running(owner_pid):
        raise RuntimeRestartError("saved bridge owner relationship cannot be proven")

    current_pid = os.getpid()
    current_parent = os.getppid()
    if current_pid == bridge_pid:
        # Historical/native topology: owner -> MCP runtime.
        if current_parent != owner_pid:
            raise RuntimeRestartError("saved bridge owner relationship cannot be proven")
        return watchdog

    # Windows venv topology: owner -> venv python shim (watchdog bridge_pid) ->
    # base CPython runtime (this process).  Do not accept an arbitrary descendant:
    # every hop must be exact and both KaroX roles must prove themselves from
    # their own command lines before a self-exit is authorized.
    if current_parent != bridge_pid or not process_is_running(bridge_pid):
        raise RuntimeRestartError("current process is not the saved bridge MCP child")
    if prove_bridge_process_identity(current_pid, (session_id,)) != session_id:
        raise RuntimeRestartError("current bridge runtime identity cannot be proven")
    if prove_bridge_process_identity(bridge_pid, (session_id,)) != session_id:
        raise RuntimeRestartError("saved bridge launcher identity cannot be proven")
    if _process_parent_pid(bridge_pid) != owner_pid:
        raise RuntimeRestartError("saved bridge launcher parent cannot be proven")
    if not prove_saved_bridge_owner_identity(owner_pid, saved_profile):
        raise RuntimeRestartError("saved bridge owner identity cannot be proven")
    return watchdog


def _default_activity_snapshot() -> dict[str, int]:
    # Lazy import avoids a module cycle through hosted_tools_runtime.
    from .proxy_server import transport_activity_snapshot

    return transport_activity_snapshot()


def _update_receipt_status(path: Path, status: str, **extra: Any) -> None:
    value = _read_json(path)
    if not value:
        return
    value["status"] = status
    value["updated_at"] = time.time()
    value.update(extra)
    try:
        _write_json(path, value)
    except OSError:
        pass


def _await_transport_idle_then_exit(
    receipt_path: Path,
    *,
    baseline_completed: int,
    activity_snapshot: Callable[[], Mapping[str, Any]],
    exit_process: Callable[[int], Any],
    timeout_seconds: float = RESTART_IDLE_TIMEOUT_SECONDS,
    poll_seconds: float = RESTART_POLL_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Exit only after a post-request response completed and HTTP is idle."""
    sleep(RESTART_MIN_GRACE_SECONDS)
    deadline = monotonic() + max(0.1, float(timeout_seconds))
    while monotonic() < deadline:
        snapshot = dict(activity_snapshot())
        completed = snapshot.get("responses_completed")
        active = snapshot.get("active_requests")
        if (
            isinstance(completed, int)
            and not isinstance(completed, bool)
            and completed > baseline_completed
            and active == 0
        ):
            _update_receipt_status(
                receipt_path,
                "triggered",
                triggered_at=time.time(),
                exit_code=SELF_RESTART_EXIT_CODE,
            )
            exit_process(SELF_RESTART_EXIT_CODE)
            return
        sleep(max(0.01, float(poll_seconds)))
    _update_receipt_status(receipt_path, "transport_busy_timeout", timed_out_at=time.time())


def _await_transport_idle_then_handoff(
    receipt_path: Path,
    *,
    session_id: str,
    owner_pid: int,
    bridge_pid: int,
    request_key_sha256: str,
    baseline_completed: int,
    activity_snapshot: Callable[[], Mapping[str, Any]],
    exit_process: Callable[[int], Any],
    timeout_seconds: float = RESTART_IDLE_TIMEOUT_SECONDS,
    handoff_timeout_seconds: float = RESTART_HANDOFF_TIMEOUT_SECONDS,
    poll_seconds: float = RESTART_POLL_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Ask the owner for a rolling Tailscale handoff after the response is safe.

    The current child stays alive while the owner warms another child and moves
    the already-owned Funnel route. Once the owner reports ``route_switched`` no
    new public request should enter this child; wait for any pre-switch request to
    drain, then exit. If the owner cannot perform the handoff, fall back to the
    historical owner-respawn exit so a restart request never becomes a no-op.
    """

    sleep(RESTART_MIN_GRACE_SECONDS)
    idle_deadline = monotonic() + max(0.1, float(timeout_seconds))
    while monotonic() < idle_deadline:
        snapshot = dict(activity_snapshot())
        completed = snapshot.get("responses_completed")
        active = snapshot.get("active_requests")
        if (
            isinstance(completed, int)
            and not isinstance(completed, bool)
            and completed > baseline_completed
            and active == 0
        ):
            try:
                request_rolling_restart(
                    session_id=session_id,
                    owner_pid=owner_pid,
                    bridge_pid=bridge_pid,
                    request_key_sha256=request_key_sha256,
                )
            except BridgeHandoffError:
                _update_receipt_status(
                    receipt_path,
                    "rolling_handoff_unavailable",
                    fallback_at=time.time(),
                )
                exit_process(SELF_RESTART_EXIT_CODE)
                return
            _update_receipt_status(
                receipt_path,
                "handoff_requested",
                handoff_requested_at=time.time(),
            )
            break
        sleep(max(0.01, float(poll_seconds)))
    else:
        _update_receipt_status(receipt_path, "transport_busy_timeout", timed_out_at=time.time())
        return

    handoff_deadline = monotonic() + max(0.1, float(handoff_timeout_seconds))
    route_switched = False
    while monotonic() < handoff_deadline:
        request = read_rolling_restart_request(
            session_id,
            owner_pid=owner_pid,
            bridge_pid=bridge_pid,
            request_key_sha256=request_key_sha256,
        )
        status = request.get("status") if request else None
        if status in {"route_switched", "completed"}:
            route_switched = True
            snapshot = dict(activity_snapshot())
            if snapshot.get("active_requests") == 0:
                _update_receipt_status(
                    receipt_path,
                    "triggered",
                    triggered_at=time.time(),
                    exit_code=SELF_RESTART_EXIT_CODE,
                    rolling_handoff=True,
                )
                exit_process(SELF_RESTART_EXIT_CODE)
                return
        elif status == "failed":
            break
        sleep(max(0.01, float(poll_seconds)))

    # If the route already moved, exiting is safe even if one stale transport
    # counter never drained; the public path is on the replacement child. If the
    # owner failed before moving the route, the standard owner-respawn path is the
    # conservative fallback and preserves the original restart semantics.
    _update_receipt_status(
        receipt_path,
        "rolling_handoff_timeout" if route_switched else "rolling_handoff_failed",
        fallback_at=time.time(),
        exit_code=SELF_RESTART_EXIT_CODE,
    )
    exit_process(SELF_RESTART_EXIT_CODE)


def schedule_saved_bridge_child_restart(
    *,
    session_id: str,
    saved_profile: str,
    idempotency_key: str,
    reason: str = "",
    browser_backend: str = "none",
    browser_process_preserved: bool = False,
    activity_snapshot: Optional[Callable[[], Mapping[str, Any]]] = None,
    exit_process: Optional[Callable[[int], Any]] = None,
) -> dict[str, Any]:
    """Schedule one owner-recoverable MCP-child recycle after this response."""
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
        raise RuntimeRestartError("runtime restart requires a stable idempotency key")
    if not isinstance(reason, str) or len(reason) > _MAX_REASON_LENGTH:
        raise RuntimeRestartError("runtime restart reason must be a string up to 500 characters")
    if browser_backend not in {"none", "playwright", "extension"}:
        raise RuntimeRestartError("runtime restart browser backend is invalid")
    if not isinstance(browser_process_preserved, bool):
        raise RuntimeRestartError("runtime restart browser preservation flag is invalid")

    watchdog = _validate_current_saved_child(session_id, saved_profile)
    receipt_path = _receipt_path(session_id, idempotency_key)
    digest = _request_digest(saved_profile, reason)
    request_key_sha256 = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    owner_pid = watchdog.get("owner_pid")
    recorded_bridge_pid = watchdog.get("bridge_pid")
    rolling_supported = (
        watchdog.get("tunnel") == "tailscale"
        and isinstance(owner_pid, int)
        and not isinstance(owner_pid, bool)
        and owner_pid > 0
        and isinstance(recorded_bridge_pid, int)
        and not isinstance(recorded_bridge_pid, bool)
        and recorded_bridge_pid > 0
        and bool(watchdog.get("public_url"))
    )
    public_result = {
        "ok": True,
        "status": "restart_scheduled",
        "session_id": session_id,
        "saved_profile": saved_profile,
        "public_url_preserved": bool(watchdog.get("public_url")),
        "session_identity_preserved": True,
        "bridge_credential_preserved": True,
        "owner_process_preserved": True,
        "browser_backend": browser_backend,
        "browser_process_preserved": browser_process_preserved,
        "restart_mode": "rolling_tailscale" if rolling_supported else "owner_respawn",
        "exit_code": SELF_RESTART_EXIT_CODE,
    }
    receipt = {
        "schema_version": 1,
        "session_id": session_id,
        "saved_profile": saved_profile,
        "request_digest": digest,
        "request_key_sha256": request_key_sha256,
        "bridge_pid": os.getpid(),
        "owner_pid": owner_pid,
        "requested_at": time.time(),
        "updated_at": time.time(),
        "attempt": 1,
        "status": "scheduled",
        "public_result": public_result,
    }
    idempotent_replay = False
    rearmed_after_timeout = False
    if not _create_receipt_exclusive(receipt_path, receipt):
        existing = _read_json(receipt_path)
        if not existing:
            raise RuntimeRestartError("runtime restart receipt is unreadable; refusing duplicate exit")
        if existing.get("request_digest") != digest:
            raise RuntimeRestartError("runtime restart idempotency key was reused with different arguments")
        existing_result = existing.get("public_result")
        if not isinstance(existing_result, dict):
            raise RuntimeRestartError("runtime restart receipt is invalid")
        if existing.get("status") != "transport_busy_timeout":
            return {
                **existing_result,
                "idempotent_replay": True,
                "receipt_status": existing.get("status"),
            }
        prior_attempt = existing.get("attempt", 1)
        if not isinstance(prior_attempt, int) or isinstance(prior_attempt, bool) or prior_attempt < 1:
            prior_attempt = 1
        next_attempt = prior_attempt + 1
        if not _claim_timeout_reschedule(receipt_path, next_attempt):
            current = _read_json(receipt_path) or existing
            current_result = current.get("public_result")
            if not isinstance(current_result, dict):
                raise RuntimeRestartError("runtime restart receipt is invalid")
            return {
                **current_result,
                "idempotent_replay": True,
                "receipt_status": current.get("status"),
            }
        existing.update(
            {
                "attempt": next_attempt,
                "bridge_pid": os.getpid(),
                "owner_pid": watchdog.get("owner_pid"),
                "status": "scheduled",
                "updated_at": time.time(),
                "retry_scheduled_at": time.time(),
            }
        )
        try:
            _write_json(receipt_path, existing)
        except OSError as exc:
            raise RuntimeRestartError("runtime restart retry receipt could not be persisted") from exc
        idempotent_replay = True
        rearmed_after_timeout = True

    snapshot_reader = activity_snapshot or _default_activity_snapshot
    snapshot = dict(snapshot_reader())
    completed = snapshot.get("responses_completed", 0)
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 0:
        completed = 0
    process_exit = exit_process or os._exit
    if rolling_supported:
        assert isinstance(owner_pid, int)
        assert isinstance(recorded_bridge_pid, int)
        worker_target: Callable[..., None] = _await_transport_idle_then_handoff
        worker_kwargs: dict[str, Any] = {
            "receipt_path": receipt_path,
            "session_id": session_id,
            "owner_pid": owner_pid,
            "bridge_pid": recorded_bridge_pid,
            "request_key_sha256": request_key_sha256,
            "baseline_completed": completed,
            "activity_snapshot": snapshot_reader,
            "exit_process": process_exit,
        }
    else:
        worker_target = _await_transport_idle_then_exit
        worker_kwargs = {
            "receipt_path": receipt_path,
            "baseline_completed": completed,
            "activity_snapshot": snapshot_reader,
            "exit_process": process_exit,
        }
    worker = threading.Thread(
        target=worker_target,
        kwargs=worker_kwargs,
        name=f"karox-self-restart-{session_id[:24]}",
        daemon=True,
    )
    worker.start()
    return {
        **public_result,
        "idempotent_replay": idempotent_replay,
        "receipt_status": "scheduled",
        "rearmed_after_timeout": rearmed_after_timeout,
    }


__all__ = [
    "RESTART_IDLE_TIMEOUT_SECONDS",
    "RuntimeRestartError",
    "SELF_RESTART_EXIT_CODE",
    "_await_transport_idle_then_exit",
    "_await_transport_idle_then_handoff",
    "schedule_saved_bridge_child_restart",
]
