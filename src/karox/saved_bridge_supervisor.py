"""Credential-free supervisor for durable saved web bridges.

A saved bridge has two layers of recovery:

* ``run_web_bridge`` owns the public route and restarts its local MCP child.
* this module is a detached sibling that restarts the saved bridge when the
  owner process itself disappears.

The supervisor never resolves bridge credentials or OAuth state.  It only reads
saved profile metadata, verifies process identity, checks port ownership, and
asks the canonical ``start_saved_bridge`` service to restore the same durable
profile.  A persistent desired-state flag makes an explicit user Stop final:
the supervisor must never resurrect a connection the user intentionally stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .detached_process import detached_flags, spawn_detached
from .paths import runtime_dir
from .process_identity import process_is_running, read_process_create_time_ns

SUPERVISOR_SCHEMA_VERSION = 1
SUPERVISOR_PROTOCOL_VERSION = 2
SUPERVISOR_POLL_SECONDS = 2.0
SUPERVISOR_MIN_BACKOFF_SECONDS = 2.0
SUPERVISOR_MAX_BACKOFF_SECONDS = 60.0
SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS = 2.0
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 15.0
SUPERVISOR_MAIN_PROGRESS_STALE_SECONDS = 30.0
SUPERVISOR_TICK_BUSY_GRACE_SECONDS = 150.0
OWNER_HEARTBEAT_STALE_SECONDS = 60.0
OWNER_FORCE_STOP_TIMEOUT_SECONDS = 10.0
SUPERVISOR_FORCE_STOP_TIMEOUT_SECONDS = 5.0


def supervisor_dir() -> Path:
    return runtime_dir() / "saved-bridge-supervisors"


def supervisor_state_path(profile_name: str) -> Path:
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return supervisor_dir() / f"{digest}.json"


def supervisor_heartbeat_path(profile_name: str) -> Path:
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return supervisor_dir() / f"{digest}.heartbeat.json"


def supervisor_desired_state_path(profile_name: str) -> Path:
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return supervisor_dir() / f"{digest}.desired.json"


def _read_desired_running(profile_name: str) -> Optional[bool]:
    try:
        payload = json.loads(
            supervisor_desired_state_path(profile_name).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("desired_running") if isinstance(payload, dict) else None
    return value if isinstance(value, bool) else None


def _read_state(profile_name: str) -> dict[str, Any]:
    path = supervisor_state_path(profile_name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    if value.get("saved_profile") not in {None, profile_name}:
        value = {}
    value.setdefault("schema_version", SUPERVISOR_SCHEMA_VERSION)
    value.setdefault("saved_profile", profile_name)
    value.setdefault("desired_running", False)
    value.setdefault("supervisor_pid", None)
    value.setdefault("supervisor_create_time_ns", None)
    value.setdefault("heartbeat_at", None)
    value.setdefault("last_owner_pid", None)
    value.setdefault("restart_count", 0)
    value.setdefault("last_restart_at", None)
    value.setdefault("last_error", None)
    value.setdefault("updated_at", None)
    desired = _read_desired_running(profile_name)
    if desired is not None:
        value["desired_running"] = desired
    return value


def _write_state(profile_name: str, state: dict[str, Any]) -> None:
    path = supervisor_state_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["schema_version"] = SUPERVISOR_SCHEMA_VERSION
    payload["saved_profile"] = profile_name
    payload["updated_at"] = time.time()
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _read_supervisor_heartbeat(profile_name: str) -> dict[str, Any]:
    path = supervisor_heartbeat_path(profile_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_supervisor_heartbeat(
    profile_name: str,
    *,
    pid: int,
    create_time_ns: Optional[int],
    main_progress_at: Optional[float] = None,
    main_busy_until: Optional[float] = None,
) -> None:
    path = supervisor_heartbeat_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_now = time.time()
    payload = {
        "saved_profile": profile_name,
        "supervisor_pid": pid,
        "supervisor_create_time_ns": create_time_ns,
        "heartbeat_at": heartbeat_now,
        "main_progress_at": (
            float(main_progress_at) if main_progress_at is not None else heartbeat_now
        ),
        "main_busy_until": (
            float(main_busy_until) if main_busy_until is not None else None
        ),
        "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
    }
    temp = path.with_name(f"{path.name}.{pid}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _remove_supervisor_heartbeat_if_owned(profile_name: str, pid: int) -> None:
    path = supervisor_heartbeat_path(profile_name)
    payload = _read_supervisor_heartbeat(profile_name)
    if payload.get("supervisor_pid") != pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _supervisor_heartbeat_loop(
    profile_name: str,
    stop_event: threading.Event,
    main_progress: dict[str, float],
) -> None:
    pid = os.getpid()
    create_time = read_process_create_time_ns(pid)
    while not stop_event.is_set():
        try:
            _write_supervisor_heartbeat(
                profile_name,
                pid=pid,
                create_time_ns=create_time,
                main_progress_at=main_progress.get("at"),
                main_busy_until=main_progress.get("busy_until"),
            )
        except OSError:
            pass
        stop_event.wait(SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS)


def set_saved_bridge_desired_running(profile_name: str, desired_running: bool) -> None:
    """Persist user intent separately from supervisor telemetry."""
    path = supervisor_desired_state_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_profile": profile_name,
        "desired_running": bool(desired_running),
        "updated_at": time.time(),
    }
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)

    state = _read_state(profile_name)
    state["desired_running"] = bool(desired_running)
    if not desired_running:
        state["last_error"] = None
    _write_state(profile_name, state)

    if not desired_running:
        # An explicit Stop must also retire the OS-level revival entries; leaving
        # them would mean the machine keeps checking on a bridge the user retired.
        try:
            from .saved_bridge_autostart import remove_saved_bridge_autostart

            remove_saved_bridge_autostart(profile_name)
        except Exception:
            pass


def saved_bridge_supervisor_status(profile_name: str) -> dict[str, Any]:
    """Return supervisor state with process-identity and heartbeat liveness."""
    state = _read_state(profile_name)
    pid = state.get("supervisor_pid")
    expected = state.get("supervisor_create_time_ns")
    process_alive = False
    if isinstance(pid, int) and pid > 0 and isinstance(expected, int) and expected > 0:
        observed = read_process_create_time_ns(pid)
        process_alive = (
            process_is_running(pid)
            and observed is not None
            and int(observed) == int(expected)
        )

    heartbeat = _read_supervisor_heartbeat(profile_name)
    heartbeat_at = heartbeat.get("heartbeat_at")
    heartbeat_age: Optional[float] = None
    heartbeat_matches = (
        heartbeat.get("supervisor_pid") == pid
        and heartbeat.get("supervisor_create_time_ns") == expected
    )
    protocol_version = heartbeat.get("protocol_version")
    protocol_compatible = protocol_version == SUPERVISOR_PROTOCOL_VERSION
    if isinstance(heartbeat_at, (int, float)) and heartbeat_matches:
        heartbeat_age = max(0.0, time.time() - float(heartbeat_at))
    heartbeat_fresh = (
        heartbeat_age is not None
        and heartbeat_age <= SUPERVISOR_HEARTBEAT_STALE_SECONDS
    )
    main_progress_at = heartbeat.get("main_progress_at")
    main_progress_age: Optional[float] = None
    if isinstance(main_progress_at, (int, float)) and heartbeat_matches:
        main_progress_age = max(0.0, time.time() - float(main_progress_at))
    main_progress_fresh = (
        main_progress_age is not None
        and main_progress_age <= SUPERVISOR_MAIN_PROGRESS_STALE_SECONDS
    )
    main_busy_until = heartbeat.get("main_busy_until")
    main_busy_active = (
        isinstance(main_busy_until, (int, float))
        and heartbeat_matches
        and float(main_busy_until) >= time.time()
    )
    if main_busy_active:
        main_progress_fresh = True
    return {
        **state,
        "state_heartbeat_at": state.get("heartbeat_at"),
        "heartbeat_at": float(heartbeat_at) if isinstance(heartbeat_at, (int, float)) else None,
        "main_progress_at": (
            float(main_progress_at) if isinstance(main_progress_at, (int, float)) else None
        ),
        "supervisor_process_alive": process_alive,
        "supervisor_heartbeat_age_seconds": heartbeat_age,
        "supervisor_heartbeat_fresh": heartbeat_fresh,
        "supervisor_main_progress_age_seconds": main_progress_age,
        "supervisor_main_progress_fresh": main_progress_fresh,
        "supervisor_main_busy_until": (
            float(main_busy_until) if isinstance(main_busy_until, (int, float)) else None
        ),
        "supervisor_protocol_version": protocol_version,
        "supervisor_protocol_compatible": protocol_compatible,
        "supervisor_alive": (
            process_alive
            and heartbeat_fresh
            and main_progress_fresh
            and protocol_compatible
        ),
    }


def _read_owner_heartbeat(watchdog_path: Optional[str]) -> Optional[float]:
    if not watchdog_path:
        return None
    try:
        payload = json.loads(Path(watchdog_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("owner_heartbeat_at") if isinstance(payload, dict) else None
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _last_owner_exit(profile_name: str) -> Optional[dict[str, Any]]:
    """Read the redacted reason the previous owner of this profile stopped.

    The owner writes it on its way out. Carrying it into supervisor state is what
    turns "the bridge fell again" into a reason a user can act on.
    """
    try:
        from .web_bridge_launcher import (
            owner_exit_record_path,
            saved_web_bridge_session_id,
        )

        payload = json.loads(
            owner_exit_record_path(
                saved_web_bridge_session_id(profile_name)
            ).read_text(encoding="utf-8")
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "reason": str(payload.get("reason") or "")[:120],
        "detail": str(payload.get("detail") or "")[:300],
        "recorded_at": payload.get("recorded_at"),
    }


def _force_stop_proven_owner(pid: int) -> bool:
    """Terminate only an ownership-proven saved-bridge process tree."""
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=OWNER_FORCE_STOP_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode in {0, 128}

    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    if getpgid is None or killpg is None:
        return False
    try:
        pgid = getpgid(pid)
    except (OSError, ProcessLookupError):
        return True
    try:
        killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    deadline = time.monotonic() + OWNER_FORCE_STOP_TIMEOUT_SECONDS / 2.0
    while time.monotonic() < deadline:
        if read_process_create_time_ns(pid) is None:
            return True
        time.sleep(0.1)
    try:
        killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + OWNER_FORCE_STOP_TIMEOUT_SECONDS / 2.0
    while time.monotonic() < deadline:
        if read_process_create_time_ns(pid) is None:
            return True
        time.sleep(0.1)
    return False


def _force_stop_proven_supervisor(pid: int, expected_create_time_ns: int) -> bool:
    """Stop only the exact stale supervisor process, never its process tree."""
    if not process_is_running(pid):
        return True
    observed = read_process_create_time_ns(pid)
    if observed is None:
        # A running PID without creation-time proof is never safe to terminate.
        return False
    if int(observed) != int(expected_create_time_ns):
        return False

    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=SUPERVISOR_FORCE_STOP_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode not in {0, 128}:
            return False
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False

    deadline = time.monotonic() + SUPERVISOR_FORCE_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return True
        current = read_process_create_time_ns(pid)
        if current is not None and int(current) != int(expected_create_time_ns):
            return True
        time.sleep(0.05)
    if os.name != "nt":
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + SUPERVISOR_FORCE_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not process_is_running(pid):
                return True
            current = read_process_create_time_ns(pid)
            if current is not None and int(current) != int(expected_create_time_ns):
                return True
            time.sleep(0.05)
    return False


def _detached_flags() -> int:
    return detached_flags()


def _spawn_detached(argv: list[str]) -> tuple[Optional[Any], str]:
    """Start a supervisor detached; see ``karox.detached_process``.

    Kept as a thin seam so the supervisor's tests can inject a spawn without
    reaching into another module.
    """
    return spawn_detached(argv)


def ensure_saved_bridge_supervisor(
    profile_name: str,
    *,
    desired_running: Optional[bool] = None,
) -> Optional[int]:
    """Ensure one detached supervisor exists, without touching bridge secrets."""
    if desired_running is not None:
        set_saved_bridge_desired_running(profile_name, desired_running)

    status = saved_bridge_supervisor_status(profile_name)
    if bool(status.get("desired_running")):
        # v5 recovery is session-scoped: a detached supervisor may keep a bridge
        # healthy while the user is running KaroX, but normal Start/Restart must
        # never register KaroX in Windows logon/reboot autostart.  Older builds
        # did register per-profile Task Scheduler/Startup entries, so retire that
        # legacy state opportunistically whenever the profile is activated.
        try:
            from .saved_bridge_autostart import remove_saved_bridge_autostart

            remove_saved_bridge_autostart(profile_name)
        except Exception:
            pass
    if status["supervisor_alive"]:
        return int(status["supervisor_pid"])
    if not bool(status.get("desired_running")):
        return None

    stale_pid = status.get("supervisor_pid")
    stale_create = status.get("supervisor_create_time_ns")
    if (
        bool(status.get("supervisor_process_alive"))
        and isinstance(stale_pid, int)
        and stale_pid > 0
        and isinstance(stale_create, int)
        and stale_create > 0
    ):
        if not _force_stop_proven_supervisor(stale_pid, stale_create):
            return None
        _remove_supervisor_heartbeat_if_owned(profile_name, stale_pid)

    argv = [sys.executable, "-m", "karox.saved_bridge_supervisor", "--saved", profile_name]
    process, mechanism = _spawn_detached(argv)
    if process is None:
        state = _read_state(profile_name)
        # The only durable place a failed unattended start can explain itself.
        state["last_error"] = mechanism
        state["last_spawn_attempt_at"] = time.time()
        try:
            _write_state(profile_name, state)
        except OSError:
            pass
        return None

    create_time = read_process_create_time_ns(process.pid)
    if create_time is None:
        deadline = time.monotonic() + 1.0
        while create_time is None and time.monotonic() < deadline:
            time.sleep(0.05)
            create_time = read_process_create_time_ns(process.pid)

    state = _read_state(profile_name)
    if not bool(state.get("desired_running")):
        if isinstance(create_time, int) and create_time > 0:
            _force_stop_proven_supervisor(process.pid, create_time)
        return None
    state["supervisor_pid"] = process.pid
    state["supervisor_create_time_ns"] = create_time
    state["heartbeat_at"] = time.time()
    state["last_error"] = None
    state["supervisor_spawn"] = mechanism
    _write_state(profile_name, state)
    try:
        _write_supervisor_heartbeat(
            profile_name,
            pid=process.pid,
            create_time_ns=create_time,
        )
    except OSError:
        pass
    return process.pid


def _claim_leadership(profile_name: str) -> bool:
    """Make this process the recorded supervisor unless another proven one won."""
    me = os.getpid()
    my_create_time = read_process_create_time_ns(me)
    state = _read_state(profile_name)
    recorded_pid = state.get("supervisor_pid")
    recorded_create = state.get("supervisor_create_time_ns")
    if (
        isinstance(recorded_pid, int)
        and recorded_pid > 0
        and recorded_pid != me
        and isinstance(recorded_create, int)
        and recorded_create > 0
    ):
        observed = read_process_create_time_ns(recorded_pid)
        if (
            process_is_running(recorded_pid)
            and observed is not None
            and int(observed) == int(recorded_create)
        ):
            return False
    state["supervisor_pid"] = me
    state["supervisor_create_time_ns"] = my_create_time
    state["heartbeat_at"] = time.time()
    _write_state(profile_name, state)
    return True


def supervisor_tick(profile_name: str) -> dict[str, Any]:
    """Perform one ownership-safe supervision iteration.

    This function is intentionally side-effect bounded and separately testable.
    It never kills a foreign process. When the durable owner is absent it calls
    the canonical saved-bridge start path. A proven owner with a stale watchdog
    heartbeat is treated as hung and its owned process tree may be recycled;
    session/OAuth identity and the saved public profile remain unchanged.
    """
    state = _read_state(profile_name)
    if not bool(state.get("desired_running")):
        return {"status": "disabled"}

    from .port_ownership import check_port_ownership
    from .web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore

    try:
        profile = WebBridgeProfileStore().get(profile_name)
    except WebBridgeProfileError as exc:
        return {"status": "profile_error", "error": str(exc)[:200]}
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {"status": "profile_error", "error": type(exc).__name__}

    ownership = check_port_ownership(profile_name, port=profile.port)
    if ownership.verdict == "reuse_same_profile" and ownership.metadata.pid_proven:
        heartbeat = _read_owner_heartbeat(
            getattr(ownership.metadata, "watchdog_path", None)
        )
        if heartbeat is None:
            # Legacy owners created before heartbeat support remain compatible.
            return {
                "status": "healthy",
                "owner_pid": ownership.metadata.pid,
                "owner_heartbeat": "legacy-unavailable",
            }
        heartbeat_age = max(0.0, time.time() - heartbeat)
        if heartbeat_age <= OWNER_HEARTBEAT_STALE_SECONDS:
            return {
                "status": "healthy",
                "owner_pid": ownership.metadata.pid,
                "owner_heartbeat_age_seconds": heartbeat_age,
            }

        owner_pid = ownership.metadata.pid
        if not isinstance(owner_pid, int) or owner_pid <= 0:
            return {
                "status": "recovery_failed",
                "error": "saved bridge owner heartbeat is stale but its PID is unavailable",
            }
        if not _force_stop_proven_owner(owner_pid):
            return {
                "status": "recovery_failed",
                "error": (
                    "saved bridge owner heartbeat is stale and the proven owned "
                    "process tree could not be stopped"
                ),
            }

        release_deadline = time.monotonic() + OWNER_FORCE_STOP_TIMEOUT_SECONDS
        while time.monotonic() < release_deadline:
            ownership = check_port_ownership(profile_name, port=profile.port)
            if ownership.verdict != "reuse_same_profile":
                break
            time.sleep(0.1)
        if ownership.verdict == "reuse_same_profile":
            return {
                "status": "recovery_failed",
                "error": "stale saved bridge owner did not release its local listener",
            }

    if ownership.verdict == "unrelated_process":
        return {
            "status": "blocked_foreign_process",
            "error": ownership.reason[:200],
        }

    from .web_bridge_launcher import start_saved_bridge

    previous_owner_exit = _last_owner_exit(profile_name)
    result = start_saved_bridge(
        profile_name,
        timeout_seconds=120.0,
    )
    if result.get("action") in {"started", "reused"}:
        return {
            "status": "recovered",
            "owner_pid": result.get("owner_pid"),
            "bridge_pid": result.get("bridge_pid"),
            "previous_owner_exit": previous_owner_exit,
        }
    return {
        "status": "recovery_failed",
        "error": str(result.get("error") or "saved bridge recovery failed")[:200],
    }


def run_saved_bridge_supervisor(profile_name: str) -> int:
    if not _claim_leadership(profile_name):
        return 0

    heartbeat_stop = threading.Event()
    main_progress = {"at": time.time(), "busy_until": 0.0}
    heartbeat_thread = threading.Thread(
        target=_supervisor_heartbeat_loop,
        args=(profile_name, heartbeat_stop, main_progress),
        name=f"karox-supervisor-heartbeat-{profile_name}",
        daemon=True,
    )
    heartbeat_thread.start()

    backoff = SUPERVISOR_MIN_BACKOFF_SECONDS
    try:
        while True:
            main_progress["at"] = time.time()
            state = _read_state(profile_name)
            if not bool(state.get("desired_running")):
                return 0

            # A newer supervisor may have won a race.  Do not run two recovery loops.
            if state.get("supervisor_pid") != os.getpid():
                status = saved_bridge_supervisor_status(profile_name)
                if status["supervisor_alive"]:
                    return 0
                if not _claim_leadership(profile_name):
                    return 0

            main_progress["busy_until"] = (
                time.time() + SUPERVISOR_TICK_BUSY_GRACE_SECONDS
            )
            result = supervisor_tick(profile_name)
            main_progress["at"] = time.time()
            main_progress["busy_until"] = 0.0
            state = _read_state(profile_name)
            state["supervisor_pid"] = os.getpid()
            state["supervisor_create_time_ns"] = read_process_create_time_ns(os.getpid())
            state["heartbeat_at"] = time.time()
            if result.get("owner_pid") is not None:
                state["last_owner_pid"] = result.get("owner_pid")

            status_name = str(result.get("status") or "")
            if status_name == "recovered":
                state["restart_count"] = int(state.get("restart_count") or 0) + 1
                state["last_restart_at"] = time.time()
                state["last_error"] = None
                # Why the previous owner went away is the one fact a repeated
                # restart cycle never recorded anywhere.
                state["last_owner_exit"] = result.get("previous_owner_exit")
                backoff = SUPERVISOR_MIN_BACKOFF_SECONDS
            elif status_name == "healthy":
                state["last_error"] = None
                backoff = SUPERVISOR_MIN_BACKOFF_SECONDS
            elif status_name == "disabled":
                _write_state(profile_name, state)
                return 0
            else:
                failure_error = str(result.get("error") or status_name)[:200]
                state["last_error"] = failure_error
                state["last_failure_status"] = status_name
                state["last_failure_error"] = failure_error
                state["last_failure_at"] = time.time()
                backoff = min(SUPERVISOR_MAX_BACKOFF_SECONDS, max(backoff * 2.0, SUPERVISOR_MIN_BACKOFF_SECONDS))
            _write_state(profile_name, state)
            time.sleep(SUPERVISOR_POLL_SECONDS if status_name in {"healthy", "recovered"} else backoff)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        _remove_supervisor_heartbeat_if_owned(profile_name, os.getpid())
        state = _read_state(profile_name)
        if state.get("supervisor_pid") == os.getpid():
            state["supervisor_pid"] = None
            state["supervisor_create_time_ns"] = None
            state["heartbeat_at"] = None
            _write_state(profile_name, state)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="KaroX saved web bridge supervisor")
    parser.add_argument("--saved", required=True, dest="profile_name")
    args = parser.parse_args(argv)
    return run_saved_bridge_supervisor(args.profile_name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
