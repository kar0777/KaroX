"""KaroX-managed browser physical isolation (B6).

A KaroX-managed browser is a Chrome process the bridge itself launched, with a
per-instance ``--user-data-dir`` and a per-instance unpacked extension directory.
The bridge NEVER accepts an extension WebSocket connection from a Chrome it did
not launch (or cannot prove it launched). The user's everyday Chrome is not a
valid backend and can never be enumerated or controlled.

This module owns three things:

1. ``BrowserInstanceRegistry`` -- persistent ownership records under
   ``runtime_dir()/vnext/browser-instances/<instance-id>/ownership.json``.
2. ``TabOwnershipRegistry`` -- bridge-side per-tab ownership so ``close_tab``
   can never close a tab the agent did not create.
3. ``launch_managed_browser`` / ``verify_managed_browser`` -- launch with the
   exact owned profile and extension dir, then verify the live process still
   matches (PID + creation time + executable + argv contains user-data-dir and
   extension-dir).

Nothing here reads secrets, terminates an unproven process, or touches the
user's personal Chrome profile.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import runtime_dir
from .process_identity import (
    capture_process_identity,
    read_process_create_time_ns,
)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

INSTANCES_ROOT = "vnext/browser-instances"
LEGACY_EXTENSION_DIR = "vnext/browser-extension-mv3"
LAUNCH_RECONNECT_WINDOW_SECONDS = 30.0
OWNERSHIP_SCHEMA_VERSION = 1


class ManagedBrowserError(RuntimeError):
    """Raised when a managed-browser isolation invariant is violated."""


class ManagedBrowserReconnectError(ManagedBrowserError):
    """Raised when a claimed reconnect cannot be proven (stale/foreign)."""


class TabOwnershipError(ManagedBrowserError):
    """Raised when a tab action violates the per-tab ownership registry."""


# --------------------------------------------------------------------------- #
# Dataclasses                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ManagedBrowserInstance:
    """Per-instance identity of one KaroX-managed browser.

    Every field is credential-free. The launch nonce is a one-shot challenge
    the extension must echo in its hello; a stale nonce from a previous bridge
    instance is rejected.
    """

    instance_id: str
    saved_profile_id: str
    session_id: str
    browser_instance_id: str
    bridge_instance_id: str
    launch_nonce: str
    extension_dir: str
    user_data_dir: str
    browser_pid: int
    browser_create_time_ns: Optional[int]
    executable_path: str
    argv: tuple[str, ...]
    captured_at: float
    # Credential-free durable pause bit.  If the MCP child dies while the user
    # is completing CAPTCHA/2FA/consent, the replacement child restores this
    # flag before accepting any agent input.
    takeover_active: bool = False
    schema_version: int = OWNERSHIP_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["argv"] = list(self.argv)
        return d

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManagedBrowserInstance":
        return cls(
            instance_id=str(value["instance_id"]),
            saved_profile_id=str(value["saved_profile_id"]),
            session_id=str(value["session_id"]),
            browser_instance_id=str(value["browser_instance_id"]),
            bridge_instance_id=str(value["bridge_instance_id"]),
            launch_nonce=str(value["launch_nonce"]),
            extension_dir=str(value["extension_dir"]),
            user_data_dir=str(value["user_data_dir"]),
            browser_pid=int(value["browser_pid"]),
            browser_create_time_ns=(
                int(value["browser_create_time_ns"])
                if value.get("browser_create_time_ns") is not None
                else None
            ),
            executable_path=str(value["executable_path"]),
            argv=tuple(str(a) for a in value.get("argv", [])),
            captured_at=float(value["captured_at"]),
            takeover_active=value.get("takeover_active") is True,
            schema_version=int(value.get("schema_version", OWNERSHIP_SCHEMA_VERSION)),
        )

    def ownership_path(self) -> Path:
        return _instance_dir(self.instance_id) / "ownership.json"


@dataclass
class TabRecord:
    """Bridge-side ownership record for one tab in one managed browser."""

    tab_id: str
    browser_instance_id: str
    session_id: str
    created_by_karox: bool
    creation_sequence: int
    ownership_state: str  # "owned" | "released" | "closed"
    is_initial_tab: bool = False


# --------------------------------------------------------------------------- #
# Path helpers                                                                #
# --------------------------------------------------------------------------- #


def _instances_root() -> Path:
    return (runtime_dir() / INSTANCES_ROOT).resolve()


def _instance_dir(instance_id: str) -> Path:
    return (_instances_root() / instance_id).resolve()


def _legacy_extension_dir() -> Path:
    return (runtime_dir() / LEGACY_EXTENSION_DIR).resolve()


def instance_extension_dir(instance_id: str) -> Path:
    return (_instance_dir(instance_id) / "extension").resolve()


def instance_profile_dir(instance_id: str) -> Path:
    return (_instance_dir(instance_id) / "profile").resolve()


def find_extension_capable_chrome() -> Path:
    """Find a Chrome/Chromium executable that supports ``--load-extension``.

    Google Chrome 128+ stable blocks ``--load-extension`` and
    ``--disable-extensions-except`` for security.  Chromium (e.g. Playwright's
    Chromium build) and Chrome for Testing still support them.  This function
    prefers Playwright's Chromium when available and falls back to the system
    Chrome (which may work on older versions or non-stable channels).
    """
    # 1. Playwright's Chromium — check PLAYWRIGHT_BROWSERS_PATH and defaults.
    pw_candidates: list[str] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        pw_candidates.append(env_path)
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        pw_candidates.append(str(Path(local_app) / "ms-playwright"))
    pw_candidates.append(str(Path.home() / ".cache" / "ms-playwright"))
    for pw_path in pw_candidates:
        pw_base = Path(pw_path)
        if not pw_base.is_dir():
            continue
        # Pick the highest-numbered chromium-XXXX build.
        for chromium_dir in sorted(pw_base.glob("chromium-[0-9]*"), reverse=True):
            for exe_rel in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe"):
                exe = chromium_dir / exe_rel
                if exe.exists():
                    return exe.resolve()
    # 2. Fall back to the system Chrome (may work on older / non-stable).
    from .system_chrome import find_system_chrome
    return find_system_chrome()


# --------------------------------------------------------------------------- #
# BrowserInstanceRegistry                                                     #
# --------------------------------------------------------------------------- #


class BrowserInstanceRegistry:
    """Persistent per-instance ownership records.

    Each instance lives under ``vnext/browser-instances/<instance-id>/`` with
    its own ``ownership.json``, ``extension/`` (the unpacked extension +
    per-instance ``config.json``), and ``profile/`` (the dedicated
    ``--user-data-dir``). Parallel sessions never share a config or a profile.
    """

    def write(self, instance: ManagedBrowserInstance) -> Path:
        d = _instance_dir(instance.instance_id)
        d.mkdir(parents=True, exist_ok=True)
        path = instance.ownership_path()
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with os.fdopen(
            os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
            encoding="utf-8", newline="\n",
        ) as fh:
            json.dump(instance.to_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return path

    def read(self, instance_id: str) -> Optional[ManagedBrowserInstance]:
        path = _instance_dir(instance_id) / "ownership.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if int(data.get("schema_version", 0)) != OWNERSHIP_SCHEMA_VERSION:
            return None
        try:
            return ManagedBrowserInstance.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def list_instances(self) -> tuple[ManagedBrowserInstance, ...]:
        root = _instances_root()
        if not root.is_dir():
            return ()
        out: list[ManagedBrowserInstance] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            inst = self.read(child.name)
            if inst is not None:
                out.append(inst)
        return tuple(out)

    def find_for_session(self, session_id: str) -> tuple[ManagedBrowserInstance, ...]:
        return tuple(i for i in self.list_instances() if i.session_id == session_id)

    def delete(self, instance_id: str) -> bool:
        d = _instance_dir(instance_id)
        if not d.is_dir():
            return False
        # Never delete the user's personal Chrome profile: this directory is
        # always under runtime_dir()/vnext/browser-instances/<id>/.
        with contextlib.suppress(OSError):
            shutil.rmtree(d)
        return not d.exists()


# --------------------------------------------------------------------------- #
# TabOwnershipRegistry                                                        #
# --------------------------------------------------------------------------- #


class TabOwnershipRegistry:
    """Bridge-side per-tab ownership so close_tab can never close a foreign tab.

    The registry is in-memory for one manager; it is NOT persisted because a
    bridge restart must re-prove the browser process before re-asserting any
    tab ownership. Initial tabs are registered as ``is_initial_tab=True`` and
    are never auto-closed by the agent.
    """

    def __init__(self, browser_instance_id: str, session_id: str) -> None:
        self._browser_instance_id = browser_instance_id
        self._session_id = session_id
        self._tabs: dict[str, TabRecord] = {}
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def register_initial(self, tab_id: str) -> TabRecord:
        if tab_id in self._tabs:
            return self._tabs[tab_id]
        rec = TabRecord(
            tab_id=tab_id,
            browser_instance_id=self._browser_instance_id,
            session_id=self._session_id,
            created_by_karox=False,
            creation_sequence=self._next_sequence(),
            ownership_state="owned",
            is_initial_tab=True,
        )
        self._tabs[tab_id] = rec
        return rec

    def register_created(self, tab_id: str) -> TabRecord:
        if tab_id in self._tabs:
            raise TabOwnershipError(f"tab {tab_id} already registered")
        rec = TabRecord(
            tab_id=tab_id,
            browser_instance_id=self._browser_instance_id,
            session_id=self._session_id,
            created_by_karox=True,
            creation_sequence=self._next_sequence(),
            ownership_state="owned",
            is_initial_tab=False,
        )
        self._tabs[tab_id] = rec
        return rec

    def get(self, tab_id: str) -> Optional[TabRecord]:
        return self._tabs.get(tab_id)

    def assert_closable(self, tab_id: str) -> TabRecord:
        rec = self._tabs.get(tab_id)
        if rec is None:
            raise TabOwnershipError(
                f"tab {tab_id} is not in the KaroX tab registry; "
                "the agent may only close tabs it created in this managed browser"
            )
        if rec.ownership_state != "owned":
            raise TabOwnershipError(f"tab {tab_id} is already {rec.ownership_state}")
        if rec.is_initial_tab:
            raise TabOwnershipError(
                f"tab {tab_id} is the managed browser's initial tab and cannot be closed by the agent"
            )
        if rec.session_id != self._session_id:
            raise TabOwnershipError(
                f"tab {tab_id} belongs to session {rec.session_id}, not {self._session_id}"
            )
        if rec.browser_instance_id != self._browser_instance_id:
            raise TabOwnershipError(
                f"tab {tab_id} belongs to a different managed browser instance"
            )
        return rec

    def mark_closed(self, tab_id: str) -> None:
        rec = self._tabs.get(tab_id)
        if rec is not None:
            rec.ownership_state = "closed"

    def snapshot(self) -> tuple[TabRecord, ...]:
        return tuple(self._tabs.values())

    def created_tab_ids(self) -> tuple[str, ...]:
        return tuple(
            rec.tab_id for rec in self._tabs.values() if rec.created_by_karox
        )


# --------------------------------------------------------------------------- #
# Legacy extension dir tombstone                                              #
# --------------------------------------------------------------------------- #


def tombstone_legacy_extension_dir() -> Path:
    """Disable the legacy shared extension dir so a personal Chrome extension
    that still points at it cannot reconnect to a new bridge.

    Writes a config.json with no websocket_url and a ``disabled`` marker. The
    personal Chrome extension reads it, finds no websocket_url, and never opens
    a socket. The directory is left in place (no user data is deleted).
    """

    d = _legacy_extension_dir()
    d.mkdir(parents=True, exist_ok=True)
    config = d / "config.json"
    tombstone = {
        "disabled": True,
        "reason": "KaroX B6: this extension dir is legacy/personal-contaminated; "
                   "managed browsers use per-instance config under vnext/browser-instances/<id>/extension/",
        "websocket_url": None,
        "token": None,
        "session_id": None,
        "bridge_instance_id": None,
        "browser_instance_id": None,
        "launch_nonce": None,
    }
    config.write_text(
        json.dumps(tombstone, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config


def is_legacy_config_disabled(config_path: Path) -> bool:
    """True when a config.json is a B6 tombstone (no websocket_url)."""

    if not config_path.is_file():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("disabled")) or data.get("websocket_url") is None


# --------------------------------------------------------------------------- #
# Launch + verify                                                             #
# --------------------------------------------------------------------------- #


def _new_instance_id() -> str:
    return f"inst-{uuid.uuid4().hex[:16]}"


def _new_browser_instance_id() -> str:
    return f"browser-{uuid.uuid4().hex[:16]}"


def _new_bridge_instance_id() -> str:
    return f"bridge-{uuid.uuid4().hex[:16]}"


def _new_launch_nonce() -> str:
    # 32 hex chars is enough for a one-shot challenge; not a credential.
    return secrets.token_hex(16)


def _argv_contains(argv: tuple[str, ...], needle: str) -> bool:
    return any(needle in arg for arg in argv)


def launch_managed_browser(
    *,
    saved_profile_id: str,
    session_id: str,
    executable: Path,
    instance_id: str,
    browser_instance_id: str,
    bridge_instance_id: str,
    launch_nonce: str,
    extension_dir: Path,
    user_data_dir: Path,
    width: int = 1440,
    height: int = 900,
    start_url: str = "about:blank",
) -> tuple[subprocess.Popen[Any], ManagedBrowserInstance]:
    """Launch one KaroX-managed Chrome with the exact owned profile and
    extension dir, then capture its process identity for later verification.

    Never uses the user's personal Chrome profile: ``user_data_dir`` is always
    a per-instance directory under ``vnext/browser-instances/<id>/profile/``.
    """

    user_data_dir.mkdir(parents=True, exist_ok=True)
    extension_dir.mkdir(parents=True, exist_ok=True)
    log_path = (runtime_dir() / "vnext" / f"managed-browser-{instance_id}.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    args = [
        str(executable),
        f"--user-data-dir={user_data_dir}",
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--enable-logging=stderr",
        "--window-position=40,40",
        f"--window-size={width},{height}",
        "about:blank",
    ]
    creationflags = 0
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        # Keep background browser work unobtrusive without placing the window
        # outside the desktop. A normal taskbar click can restore it, and user
        # takeover explicitly restores/focuses the owned window.
        args.insert(-1, "--start-minimized")
        # DETACHED_PROCESS: Chrome does not inherit the launcher's console,
        # so a SIGHUP/CTRL_CLOSE on the launcher never reaches Chrome.
        # CREATE_NEW_PROCESS_GROUP: Chrome is not in the launcher's process
        # group, so CTRL+C / CTRL_BREAK signals do not cascade.
        # CREATE_BREAKAWAY_FROM_JOB: Chrome is not bound to the launcher's
        # job object, so the launcher's job-termination does not kill Chrome.
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            shell=False,
            creationflags=creationflags,
            **popen_options,
        )
    except Exception as exc:
        log_handle.close()
        raise ManagedBrowserError("KaroX-managed Chrome could not be started") from exc
    finally:
        try:
            log_handle.close()
        except Exception:
            pass

    identity = capture_process_identity(
        process.pid,
        executable=str(executable),
        argv=args,
    )
    instance = ManagedBrowserInstance(
        instance_id=instance_id,
        saved_profile_id=saved_profile_id,
        session_id=session_id,
        browser_instance_id=browser_instance_id,
        bridge_instance_id=bridge_instance_id,
        launch_nonce=launch_nonce,
        extension_dir=str(extension_dir),
        user_data_dir=str(user_data_dir),
        browser_pid=process.pid,
        browser_create_time_ns=identity.create_time_ns,
        executable_path=str(executable),
        argv=tuple(args),
        captured_at=time.time(),
    )
    return process, instance


def verify_managed_browser(
    instance: ManagedBrowserInstance,
) -> bool:
    """Verify a live process still matches the persisted managed-browser identity.

    Defeats PID reuse (create-time mismatch), wrong executable, and a command
    line that no longer contains the exact owned user-data-dir or extension dir.
    Never terminates anything.
    """

    # Liveness via create-time reader: a reused PID has a different create time.
    observed = read_process_create_time_ns(instance.browser_pid)
    if observed is None:
        return False
    if instance.browser_create_time_ns is None:
        return False
    if int(observed) != int(instance.browser_create_time_ns):
        return False
    # Argv must still mention the exact owned user-data-dir and extension dir.
    if not _argv_contains(instance.argv, instance.user_data_dir):
        return False
    if not _argv_contains(instance.argv, instance.extension_dir):
        return False
    return True


def argv_contains_user_data_dir(argv: tuple[str, ...], user_data_dir: str) -> bool:
    return _argv_contains(argv, user_data_dir)


def argv_contains_extension_dir(argv: tuple[str, ...], extension_dir: str) -> bool:
    return _argv_contains(argv, extension_dir)


# --------------------------------------------------------------------------- #
# Hello verification                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HelloVerification:
    accepted: bool
    reason: str


def verify_hello(
    hello: Mapping[str, Any],
    *,
    expected_bridge_instance_id: str,
    expected_browser_instance_id: str,
    expected_saved_profile_id: str,
    expected_session_id: str,
    expected_launch_nonce: str,
    consumed_nonces: set[str],
) -> HelloVerification:
    """Verify an extension hello against the expected managed-instance identity.

    A hello is accepted ONLY when every field matches AND the launch nonce has
    not already been consumed (one-shot, except the documented reconnect flow
    which re-issues a fresh nonce). Any mismatch fails closed.
    """

    if not isinstance(hello, Mapping):
        return HelloVerification(False, "hello is not an object")
    bridge = str(hello.get("bridge_instance_id") or "")
    if bridge != expected_bridge_instance_id:
        return HelloVerification(False, "bridge_instance_id mismatch")
    browser = str(hello.get("browser_instance_id") or "")
    if browser != expected_browser_instance_id:
        return HelloVerification(False, "browser_instance_id mismatch")
    profile = str(hello.get("saved_profile_id") or "")
    if profile != expected_saved_profile_id:
        return HelloVerification(False, "saved_profile_id mismatch")
    session = str(hello.get("session_id") or "")
    if session != expected_session_id:
        return HelloVerification(False, "session_id mismatch")
    nonce = str(hello.get("launch_nonce") or "")
    if nonce != expected_launch_nonce:
        return HelloVerification(False, "launch_nonce mismatch")
    if nonce in consumed_nonces:
        return HelloVerification(False, "launch_nonce already consumed")
    return HelloVerification(True, "verified")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


__all__ = [
    "BrowserInstanceRegistry",
    "HelloVerification",
    "LAUNCH_RECONNECT_WINDOW_SECONDS",
    "LEGACY_EXTENSION_DIR",
    "ManagedBrowserError",
    "ManagedBrowserInstance",
    "ManagedBrowserReconnectError",
    "OWNERSHIP_SCHEMA_VERSION",
    "TabOwnershipError",
    "TabOwnershipRegistry",
    "TabRecord",
    "argv_contains_extension_dir",
    "argv_contains_user_data_dir",
    "instance_extension_dir",
    "instance_profile_dir",
    "is_legacy_config_disabled",
    "launch_managed_browser",
    "tombstone_legacy_extension_dir",
    "verify_hello",
    "verify_managed_browser",
]
