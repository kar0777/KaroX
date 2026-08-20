"""Chrome-extension browser backend for persistent headed KaroX sessions.

The extension backend is intended for real user-facing browser work: Google,
Vercel, GitHub and other identity providers that may reject Playwright's bundled
Chromium.  KaroX launches the installed Chrome with a dedicated persistent
profile and an unpacked Manifest V3 extension.  The extension controls every
ordinary tab in that profile, but it cannot see the user's everyday Chrome
profile or its tabs.

Commands travel over an authenticated loopback WebSocket.  Passwords, payment
fields and credentials still require user takeover; ordinary navigation and DOM
work happen in background tabs so they do not steal focus from the user.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .artifacts import ArtifactStore
from .browser_access import (
    BrowserAccessPolicy,
    _FREE_EVIDENCE,
    _FREE_TRIAL_TEXT,
    _PAYMENT_CREDENTIAL_HINT,
    _PAYMENT_TEXT,
    _SECRET_INPUT_HINT,
    _safe_network_url,
    validate_browser_url,
)
from .browser_credential_injection import BrowserCredentialInjectionError
from .browser_credentials import BrowserCredentialStore
from .browser_session import BrowserError, BrowserSecurityError
from .managed_browser import (
    BrowserInstanceRegistry,
    ManagedBrowserInstance,
    TabOwnershipError,
    TabOwnershipRegistry,
    find_extension_capable_chrome,
    instance_extension_dir,
    instance_profile_dir,
    launch_managed_browser,
    tombstone_legacy_extension_dir,
    verify_hello,
    verify_managed_browser,
)
from .paths import runtime_dir
from .security import redact
from .system_chrome import chrome_profile_dir, find_system_chrome, terminate_chrome_process


class ExtensionBridgeError(BrowserError):
    """Raised when the local extension bridge or Chrome process fails."""


class _ExtensionBridgeServer:
    """One authenticated loopback WebSocket used by one Chrome profile."""

    def __init__(
        self,
        session_id: str,
        *,
        saved_profile_id: str,
        bridge_instance_id: str,
        browser_instance_id: str,
        launch_nonce: str,
    ) -> None:
        self.session_id = session_id
        self.saved_profile_id = saved_profile_id
        self.bridge_instance_id = bridge_instance_id
        self.browser_instance_id = browser_instance_id
        self.launch_nonce = launch_nonce
        # One-shot nonce: once an extension hello consumes it, a second hello
        # with the same nonce is rejected. Reconnect flow re-issues a fresh
        # nonce via reissue_reconnect_nonce().
        self._consumed_nonces: set[str] = set()
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self._app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self.port = int(self._socket.getsockname()[1])
        self.websocket_url = f"ws://127.0.0.1:{self.port}/extension"
        self._connected = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Optional[WebSocket] = None
        self._pending: dict[str, concurrent.futures.Future[dict[str, Any]]] = {}
        self._pending_lock = threading.RLock()
        self._hello: dict[str, Any] = {}
        self._hello_verified = False
        self._stopped = False

        @self._app.websocket("/extension")
        async def extension_socket(websocket: WebSocket) -> None:
            supplied = websocket.query_params.get("token", "")
            if supplied != self.token:
                await websocket.close(code=4403)
                return
            await websocket.accept()
            self._loop = asyncio.get_running_loop()
            self._websocket = websocket
            try:
                while True:
                    message = await websocket.receive_json()
                    if not isinstance(message, dict):
                        continue
                    kind = message.get("type")
                    if kind == "hello":
                        # B6: verify the hello against the expected managed-
                        # browser identity. A connection from a personal Chrome
                        # (or any Chrome the bridge did not launch) fails here
                        # and the socket is closed; _connected stays unset so
                        # call() never dispatches a command to it.
                        verification = verify_hello(
                            message,
                            expected_bridge_instance_id=self.bridge_instance_id,
                            expected_browser_instance_id=self.browser_instance_id,
                            expected_saved_profile_id=self.saved_profile_id,
                            expected_session_id=self.session_id,
                            expected_launch_nonce=self.launch_nonce,
                            consumed_nonces=self._consumed_nonces,
                        )
                        if not verification.accepted:
                            await websocket.close(code=4403)
                            self._websocket = None
                            return
                        self._consumed_nonces.add(self.launch_nonce)
                        self._hello_verified = True
                        self._hello = {
                            "extension_version": str(message.get("extension_version", ""))[:80],
                            "browser": str(message.get("browser", ""))[:300],
                            "session_id": str(message.get("session_id", ""))[:200],
                        }
                        # Acknowledge the verified hello so the extension only
                        # treats itself as connected after the bridge accepted it.
                        try:
                            await websocket.send_json({"type": "hello_ack"})
                        except Exception:
                            pass
                        self._connected.set()
                        continue
                    if kind == "ping":
                        continue
                    if kind != "result":
                        continue
                    request_id = message.get("id")
                    if not isinstance(request_id, str):
                        continue
                    with self._pending_lock:
                        future = self._pending.pop(request_id, None)
                    if future is None or future.done():
                        continue
                    if message.get("ok") is True:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {"value": result})
                    else:
                        future.set_exception(
                            ExtensionBridgeError(str(message.get("error") or "extension command failed"))
                        )
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                if self._websocket is websocket:
                    self._websocket = None
                    self._loop = None
                    self._connected.clear()
                self._fail_pending("KaroX browser extension disconnected")

        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._run,
            name=f"karox-extension-bridge-{session_id[:24]}",
            daemon=True,
        )

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        finally:
            try:
                self._socket.close()
            except OSError:
                pass
            self._connected.clear()
            self._fail_pending("KaroX extension bridge stopped")

    def start(self) -> None:
        self._thread.start()

    @property
    def connected(self) -> bool:
        # connected requires a verified hello: a socket that opened but sent a
        # mismatched hello is closed and never counts as connected.
        return self._connected.is_set() and self._websocket is not None and self._hello_verified

    @property
    def hello(self) -> dict[str, Any]:
        return dict(self._hello)

    def wait_connected(self, timeout: float = 20.0) -> None:
        if not self._connected.wait(timeout=max(1.0, timeout)):
            raise ExtensionBridgeError(
                "KaroX-managed Chrome did not connect with a verified hello; "
                "the bridge never accepts a connection from a Chrome it did not launch"
            )
        if not self._hello_verified:
            raise ExtensionBridgeError(
                "KaroX extension hello was rejected; this connection is not a proven managed browser"
            )

    def reissue_reconnect_nonce(self, new_nonce: str) -> None:
        # Reconnect flow: a proven surviving managed browser reconnects to a
        # new bridge instance. The new bridge re-issues a fresh launch nonce and
        # rewrites config.json so the extension echoes the new nonce in hello.
        # The old nonce stays consumed, so a replay from a stale config fails.
        self.launch_nonce = new_nonce
        self._consumed_nonces.discard(new_nonce)
        self._hello_verified = False
        self._connected.clear()

    async def _send(self, payload: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None:
            raise ExtensionBridgeError("KaroX Chrome extension is not connected")
        await websocket.send_json(payload)

    def call(self, method: str, params: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        self.wait_connected(min(max(timeout, 1.0), 30.0))
        loop = self._loop
        if loop is None:
            raise ExtensionBridgeError("KaroX Chrome extension event loop is unavailable")
        request_id = uuid.uuid4().hex
        future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        with self._pending_lock:
            self._pending[request_id] = future
        send_future = asyncio.run_coroutine_threadsafe(
            self._send(
                {
                    "type": "command",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            ),
            loop,
        )
        try:
            send_future.result(timeout=min(max(timeout, 1.0), 15.0))
            return future.result(timeout=max(1.0, timeout))
        except concurrent.futures.TimeoutError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise ExtensionBridgeError(f"browser extension command timed out: {method}") from exc
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ExtensionBridgeError(message))

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        loop = self._loop
        websocket = self._websocket
        if loop is not None and websocket is not None:
            try:
                asyncio.run_coroutine_threadsafe(websocket.close(code=1001), loop).result(timeout=2.0)
            except Exception:
                pass
        self._server.should_exit = True
        self._thread.join(timeout=5.0)
        try:
            self._socket.close()
        except OSError:
            pass
        self._fail_pending("KaroX extension bridge closed")


def _extension_source_files() -> tuple[str, ...]:
    return (
        "manifest.json",
        "service_worker.js",
        "content.js",
        "page_console_bridge.js",
        "dom_helpers.js",
        "newtab.html",
        "newtab.css",
        "newtab.js",
        "sidepanel.html",
        "sidepanel.css",
        "sidepanel.js",
    )


def _prepare_extension(
    server: _ExtensionBridgeServer,
    *,
    instance: ManagedBrowserInstance,
) -> Path:
    # Per-instance extension dir under vnext/browser-instances/<id>/extension/.
    # Parallel sessions never share a config.json; the personal Chrome's
    # legacy extension dir is tombstoned separately and never receives a
    # working websocket_url/token.
    destination = instance_extension_dir(instance.instance_id)
    destination.mkdir(parents=True, exist_ok=True)
    package_root = resources.files("karox.browser_extension")
    for name in _extension_source_files():
        source = package_root.joinpath(name)
        destination.joinpath(name).write_bytes(source.read_bytes())
    config = {
        "session_id": server.session_id,
        "websocket_url": server.websocket_url,
        "token": server.token,
        "bridge_instance_id": server.bridge_instance_id,
        "browser_instance_id": server.browser_instance_id,
        "saved_profile_id": server.saved_profile_id,
        "launch_nonce": server.launch_nonce,
        "instance_id": instance.instance_id,
    }
    destination.joinpath("config.json").write_text(
        json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def _launch_extension_chrome(
    extension_dir: Path,
    *,
    width: int,
    height: int,
) -> tuple[subprocess.Popen[Any], Path]:
    executable = find_system_chrome()
    profile = chrome_profile_dir()
    log_path = (runtime_dir() / "vnext" / "extension-chrome.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    installed_marker = extension_dir / ".installed-in-karo-profile"
    start_url = "about:blank" if installed_marker.is_file() else "chrome://extensions/"
    args = [
        str(executable),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        f"--window-size={width},{height}",
        start_url,
    ]
    creationflags = 0
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
    else:
        # The managed bridge itself is a process-group leader. Without a new
        # session, stopping that group also kills Chrome even when cleanup only
        # detaches the extension bridge.
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
        raise ExtensionBridgeError("Google Chrome could not be started for KaroX") from exc
    finally:
        log_handle.close()
    return process, profile


def _show_extension_setup(extension_dir: Path) -> None:
    """Open the unpacked extension folder beside Chrome's extension page."""
    if os.name == "nt":
        try:
            os.startfile(str(extension_dir))  # type: ignore[attr-defined]
        except OSError:
            pass
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        executable = shutil.which(opener)
        if executable:
            try:
                subprocess.Popen(
                    [executable, str(extension_dir)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except OSError:
                pass


def _profile_chrome_process_ids(profile: Path) -> tuple[int, ...]:
    """Find only Chrome processes whose command line names the KaroX profile."""
    profile_text = str(profile.resolve())
    found: set[int] = set()
    if os.name == "nt":
        environment = dict(os.environ)
        environment["KAROX_PROFILE_NEEDLE"] = profile_text
        script = (
            "$needle=$env:KAROX_PROFILE_NEEDLE; "
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
                env=environment,
                # This is a background ownership probe. On Windows, spawning
                # powershell.exe without CREATE_NO_WINDOW flashes a console
                # window every time browser lifecycle/status is refreshed.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        for line in completed.stdout.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid > 0:
                found.add(pid)
        return tuple(sorted(found))
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace"
                )
                pid = int(entry.name)
            except (OSError, ValueError):
                continue
            if profile_text in command and "chrome" in command.lower():
                found.add(pid)
        return tuple(sorted(found))
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or profile_text not in parts[1] or "chrome" not in parts[1].lower():
            continue
        try:
            found.add(int(parts[0]))
        except ValueError:
            pass
    return tuple(sorted(found))


def _terminate_profile_chrome(profile: Path) -> bool:
    """Terminate the dedicated profile without touching the user's normal Chrome."""
    pids = _profile_chrome_process_ids(profile)
    if not pids:
        return False
    if os.name == "nt":
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        remaining = _profile_chrome_process_ids(profile)
        if not remaining:
            return True
        time.sleep(0.05)
    for pid in _profile_chrome_process_ids(profile):
        try:
            # SIGKILL does not exist on Windows; SIGTERM is what os.kill maps
            # to a hard TerminateProcess there, so resolve it at runtime.
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    return True


class ChromeExtensionBrowserSessionManager:
    """Browser manager with the same hosted-tool contract as the Playwright manager."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        policy: BrowserAccessPolicy,
        credential_store: Optional[BrowserCredentialStore] = None,
    ) -> None:
        if artifacts.session_id != policy.session_id:
            raise ValueError("browser policy and artifact store session IDs differ")
        if not policy.headed or not policy.user_takeover:
            raise ValueError("extension browser requires headed user-takeover mode")
        self._artifacts = artifacts
        self.policy = policy
        self._credential_store = credential_store or BrowserCredentialStore()
        self._bridge: Optional[_ExtensionBridgeServer] = None
        self._process: Optional[subprocess.Popen[Any]] = None
        self._instance: Optional[ManagedBrowserInstance] = None
        self._registry = BrowserInstanceRegistry()
        self._tab_registry: Optional[TabOwnershipRegistry] = None
        self._context_id = f"ctx-ext-{uuid.uuid4().hex[:16]}"
        self._takeover_active = False
        self._lock = threading.RLock()
        # Saved profile id: the saved bridge profile name (or "ad-hoc" when no
        # saved profile is bound). Used in hello verification so a connection
        # from a different saved profile fails closed.
        self._saved_profile_id = getattr(policy, "saved_profile_id", "ad-hoc") or "ad-hoc"

    @property
    def is_open(self) -> bool:
        # B6: a managed browser is open ONLY when a verified bridge connection
        # exists AND the persisted ownership record still describes the live
        # process (PID + creation time + argv + user-data-dir + extension dir).
        # A connection from a personal Chrome is never accepted (hello is
        # rejected in _ExtensionBridgeServer), so it cannot satisfy is_open.
        if self._bridge is None or not self._bridge.connected:
            return False
        if self._instance is None:
            return False
        return verify_managed_browser(self._instance)

    def _ensure_started(self, width: int = 1440, height: int = 900) -> None:
        # B6: the bridge NEVER accepts a pre-existing connection from a Chrome
        # it did not launch. Two flows only:
        #   1. A proven reconnect: a prior ownership record still matches the
        #      live process (PID + create-time + argv + dirs). The bridge
        #      re-issues a fresh launch nonce, rewrites the per-instance
        #      config.json, and waits for the surviving managed Chrome to
        #      reconnect with a verified hello.
        #   2. A fresh launch: the bridge launches a new KaroX-managed Chrome
        #      with its own --user-data-dir and --load-extension, captures its
        #      process identity, persists ownership, and waits for hello.
        # The legacy shared extension dir is tombstoned so a personal Chrome
        # extension can never reconnect to a new bridge.
        if self.is_open:
            return
        with self._lock:
            tombstone_legacy_extension_dir()
            if self._process is not None:
                terminate_chrome_process(self._process)
                self._process = None
            if self._bridge is not None:
                self._bridge.stop()
                self._bridge = None
            self._instance = None
            self._tab_registry = None

            # Reconnect only a browser owned by BOTH this durable session and
            # this saved profile. A reused session id must never let one saved
            # profile adopt or delete another profile's managed Chrome.
            prior_instances = self._registry.find_for_session(self.policy.session_id)
            profile_instances = tuple(
                instance
                for instance in prior_instances
                if instance.saved_profile_id == self._saved_profile_id
            )
            proven_prior = next(
                (i for i in profile_instances if verify_managed_browser(i)), None,
            )
            if proven_prior is not None:
                self._launch_or_reconnect_proven(proven_prior, width, height)
            else:
                # Delete only stale records owned by this exact saved profile;
                # foreign-profile records remain untouched and fail closed.
                for stale in profile_instances:
                    self._registry.delete(stale.instance_id)
                self._launch_fresh(width, height)

    def _launch_or_reconnect_proven(
        self, prior: ManagedBrowserInstance, width: int, height: int,
    ) -> None:
        # The prior managed Chrome process is alive and proven. Start a fresh
        # bridge with a fresh launch nonce, rewrite the per-instance config.json
        # so the extension echoes the new nonce, then wait for hello.
        new_nonce = secrets.token_hex(16)
        new_bridge_instance_id = f"bridge-{uuid.uuid4().hex[:16]}"
        bridge = _ExtensionBridgeServer(
            self.policy.session_id,
            saved_profile_id=prior.saved_profile_id,
            bridge_instance_id=new_bridge_instance_id,
            browser_instance_id=prior.browser_instance_id,
            launch_nonce=new_nonce,
        )
        bridge.start()
        # Update the persisted ownership record with the new bridge instance id
        # and launch nonce so a later reconnect can verify again.
        updated = ManagedBrowserInstance(
            instance_id=prior.instance_id,
            saved_profile_id=prior.saved_profile_id,
            session_id=prior.session_id,
            browser_instance_id=prior.browser_instance_id,
            bridge_instance_id=new_bridge_instance_id,
            launch_nonce=new_nonce,
            extension_dir=prior.extension_dir,
            user_data_dir=prior.user_data_dir,
            browser_pid=prior.browser_pid,
            browser_create_time_ns=prior.browser_create_time_ns,
            executable_path=prior.executable_path,
            argv=prior.argv,
            captured_at=time.time(),
            takeover_active=prior.takeover_active,
        )
        self._registry.write(updated)
        # _prepare_extension rewrites the per-instance config.json so the
        # surviving managed Chrome echoes the new launch nonce in hello.
        _prepare_extension(bridge, instance=updated)
        self._bridge = bridge
        self._instance = updated
        # Fail closed across MCP-child restart: takeover is durable in the
        # proven managed-browser ownership record and restored before any
        # post-reconnect agent action can run.
        self._takeover_active = updated.takeover_active
        self._tab_registry = TabOwnershipRegistry(updated.browser_instance_id, updated.session_id)
        try:
            bridge.wait_connected(15.0)
        except ExtensionBridgeError as exc:
            # Reconnect failed: the surviving Chrome did not re-hello in time.
            # Fail closed; do NOT fall back to a personal Chrome connection.
            bridge.stop()
            self._bridge = None
            self._instance = None
            self._tab_registry = None
            raise ExtensionBridgeError(
                "KaroX-managed browser reconnect failed; the bridge never falls back to a personal Chrome. "
                "Restart the browser session to launch a fresh managed Chrome."
            ) from exc

    def _launch_fresh(self, width: int, height: int) -> None:
        import secrets as _secrets
        instance_id = f"inst-{uuid.uuid4().hex[:16]}"
        browser_instance_id = f"browser-{uuid.uuid4().hex[:16]}"
        bridge_instance_id = f"bridge-{uuid.uuid4().hex[:16]}"
        launch_nonce = _secrets.token_hex(16)
        # The startup URL is the target work page — no about:blank, no second
        # tab. The single startup tab is registered as created_by_karox.
        startup_url = getattr(self.policy, "startup_url", None) or "about:blank"
        bridge = _ExtensionBridgeServer(
            self.policy.session_id,
            saved_profile_id=self._saved_profile_id,
            bridge_instance_id=bridge_instance_id,
            browser_instance_id=browser_instance_id,
            launch_nonce=launch_nonce,
        )
        bridge.start()
        executable = find_extension_capable_chrome()
        ext_dir = instance_extension_dir(instance_id)
        profile_dir = instance_profile_dir(instance_id)
        # Pre-write the extension assets + config.json so --load-extension finds them.
        # We need a placeholder instance for _prepare_extension; build it first.
        placeholder = ManagedBrowserInstance(
            instance_id=instance_id,
            saved_profile_id=self._saved_profile_id,
            session_id=self.policy.session_id,
            browser_instance_id=browser_instance_id,
            bridge_instance_id=bridge_instance_id,
            launch_nonce=launch_nonce,
            extension_dir=str(ext_dir),
            user_data_dir=str(profile_dir),
            browser_pid=0,
            browser_create_time_ns=None,
            executable_path=str(executable),
            argv=(),
            captured_at=time.time(),
        )
        _prepare_extension(bridge, instance=placeholder)
        process, instance = launch_managed_browser(
            saved_profile_id=self._saved_profile_id,
            session_id=self.policy.session_id,
            executable=executable,
            instance_id=instance_id,
            browser_instance_id=browser_instance_id,
            bridge_instance_id=bridge_instance_id,
            launch_nonce=launch_nonce,
            extension_dir=ext_dir,
            user_data_dir=profile_dir,
            width=width,
            height=height,
            start_url=startup_url,
        )
        self._registry.write(instance)
        self._bridge = bridge
        self._process = process
        self._instance = instance
        self._tab_registry = TabOwnershipRegistry(instance.browser_instance_id, instance.session_id)
        try:
            bridge.wait_connected(45.0)
        except ExtensionBridgeError as exc:
            code = process.poll()
            hello_received = dict(bridge._hello) if bridge._hello else {}
            hello_received.pop("token", None)
            if "launch_nonce" in hello_received:
                hello_received["launch_nonce"] = "***MASKED***"
            bridge.stop()
            terminate_chrome_process(process)
            self._bridge = None
            self._process = None
            self._instance = None
            self._tab_registry = None
            if code is not None:
                raise ExtensionBridgeError(
                    f"KaroX-managed Chrome exited before the extension connected (exit={code}); "
                    "the bridge never falls back to a personal Chrome"
                ) from exc
            detail = (
                f"hello_received={bool(hello_received)} "
                f"hello_verified={bridge._hello_verified} "
                f"hello_echoed={sorted(hello_received.keys()) if hello_received else []}"
            )
            raise ExtensionBridgeError(
                f"KaroX-managed Chrome did not connect with a verified hello ({detail}); "
                "the bridge never falls back to a personal Chrome"
            ) from exc
        # B6: Chrome was launched with about:blank so it created exactly one
        # tab. navigate_to (in the service worker) navigates that tab to the
        # startup URL — 1 tab created, 1 tab navigated, 0 tabs closed.
        if self._tab_registry is not None and startup_url != "about:blank":
            try:
                nav_result = bridge.call(
                    "navigate_to", {"url": startup_url}, 30,
                )
                startup_tab_id = str(nav_result.get("tab_id") or "")
                if startup_tab_id:
                    self._tab_registry.register_created(startup_tab_id)
                # Keep the managed browser minimized/backgrounded after startup.
                # The user can restore it from the taskbar, and explicit takeover
                # is the only flow that is allowed to focus/raise the window.
            except Exception:
                pass

    @property
    def takeover_active(self) -> bool:
        if self._takeover_active:
            return True
        instance = self._instance
        if instance is not None and instance.takeover_active:
            return True
        # After an unexpected MCP-child crash the new manager has not connected
        # to the surviving Chrome yet. Consult only a proven live ownership
        # record for this exact session + saved profile, so user takeover remains
        # fail-closed before the first reconnect/browser action.
        for prior in self._registry.find_for_session(self.policy.session_id):
            if (
                prior.saved_profile_id == self._saved_profile_id
                and prior.takeover_active
                and verify_managed_browser(prior)
            ):
                return True
        return False

    def _persist_takeover_active(self, active: bool) -> None:
        """Persist the fail-closed user-control bit in browser ownership state."""
        instance = self._instance
        if instance is None:
            raise BrowserSecurityError(
                "managed browser ownership is unavailable; takeover state cannot be changed safely"
            )
        updated = ManagedBrowserInstance(
            instance_id=instance.instance_id,
            saved_profile_id=instance.saved_profile_id,
            session_id=instance.session_id,
            browser_instance_id=instance.browser_instance_id,
            bridge_instance_id=instance.bridge_instance_id,
            launch_nonce=instance.launch_nonce,
            extension_dir=instance.extension_dir,
            user_data_dir=instance.user_data_dir,
            browser_pid=instance.browser_pid,
            browser_create_time_ns=instance.browser_create_time_ns,
            executable_path=instance.executable_path,
            argv=instance.argv,
            captured_at=time.time(),
            takeover_active=active,
            schema_version=instance.schema_version,
        )
        self._registry.write(updated)
        self._instance = updated

    @property
    def context_id(self) -> str:
        return self._context_id

    @property
    def engine(self) -> str:
        return "chrome_extension_mv3"

    def _timeout(self, deadline_seconds: float, *, cap: float = 60.0) -> float:
        return max(1.0, min(float(deadline_seconds), cap))

    def _call(self, method: str, params: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._ensure_started()
        assert self._bridge is not None
        return self._bridge.call(method, params, self._timeout(deadline_seconds))

    def _assert_agent_input_allowed(self) -> None:
        if self.takeover_active:
            raise BrowserSecurityError(
                "browser input is paused while the user has control; call resume_after_user_takeover after the user finishes"
            )

    @staticmethod
    def _selector(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 2000:
            raise BrowserError("selector must be a 1-2000 character string")
        return value

    def _inspect(self, selector: str, deadline_seconds: float) -> dict[str, Any]:
        result = self._call("inspect", {"selector": selector}, deadline_seconds)
        return {str(key): value for key, value in result.items()}

    def _assert_action_safe(self, metadata: Mapping[str, Any]) -> None:
        if not metadata:
            raise BrowserSecurityError("browser target could not be inspected safely; use user takeover")
        text = " ".join(str(value) for value in metadata.values())
        payment_like = bool(_PAYMENT_TEXT.search(text))
        free_trial = bool(_FREE_TRIAL_TEXT.search(text))
        if payment_like and not free_trial and not self.policy.payment_confirmation:
            raise BrowserSecurityError(
                "payment or subscription action requires explicit payment permission or user takeover"
            )
        if free_trial and not self.policy.payment_confirmation:
            evidence = f"{metadata.get('context', '')}\n{text}"
            if not _FREE_EVIDENCE.search(evidence):
                raise BrowserSecurityError(
                    "free-trial action is ambiguous; use user takeover"
                )

    def open(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            url = validate_browser_url(arguments.get("url"), self.policy)
            width = int(arguments.get("width", 1440))
            height = int(arguments.get("height", 900))
            if not 320 <= width <= 3840 or not 240 <= height <= 2160:
                raise BrowserError("browser viewport is outside safe bounds")
            reused = self.is_open
            self._ensure_started(width, height)
            self._assert_agent_input_allowed()
            result = self._call("open", {"url": url}, deadline_seconds)
            # The first navigation lands on the managed browser's initial tab;
            # register it as an initial tab (never auto-closed by the agent).
            tab_id = result.get("tab_id")
            if tab_id and self._tab_registry is not None and self._tab_registry.get(str(tab_id)) is None:
                self._tab_registry.register_initial(str(tab_id))
            return {
                **result,
                "reused": reused,
                "session_id": self.policy.session_id,
                "context_id": self._context_id,
                "context_preserved": True,
                "context_isolated": True,
                "headed": True,
                "engine": self.engine,
                "profile_persistent": True,
            }

    def recover_if_blank(self, deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            if not self.is_open or self._takeover_active:
                return {"recovered": False, "context_id": self._context_id}
            return {
                **self._call("recover", {}, deadline_seconds),
                "context_id": self._context_id,
            }

    def tabs(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            result = self._call("tabs", {}, deadline_seconds)
            # Reconcile the tab registry with the live tabs reported by the
            # extension. Any tab the extension sees that the bridge does not
            # know about is registered as an INITIAL tab (never auto-closed).
            # The agent may only close tabs it explicitly created via new_tab.
            if self._tab_registry is not None:
                for row in result.get("tabs", []):
                    tid = str(row.get("tab_id") or "")
                    if tid and self._tab_registry.get(tid) is None:
                        self._tab_registry.register_initial(tid)
                # Drop records for tabs the extension no longer reports (user
                # closed them manually); mark them closed in the registry.
                live_ids = {str(row.get("tab_id")) for row in result.get("tabs", []) if row.get("tab_id")}
                for rec in self._tab_registry.snapshot():
                    if rec.tab_id not in live_ids and rec.ownership_state == "owned":
                        self._tab_registry.mark_closed(rec.tab_id)
            return {
                **result,
                "context_id": self._context_id,
                "takeover_active": self._takeover_active,
                "engine": self.engine,
                "profile_persistent": True,
            }

    def new_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            raw = arguments.get("url")
            params: dict[str, Any] = {}
            if raw is not None:
                params["url"] = validate_browser_url(raw, self.policy)
            result = self._call("new_tab", params, deadline_seconds)
            tab_id = result.get("tab_id")
            if tab_id and self._tab_registry is not None:
                self._tab_registry.register_created(str(tab_id))
            return result

    def switch_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            tab_id = arguments.get("tab_id")
            if not isinstance(tab_id, str):
                raise BrowserError("tab_id must be a string")
            # switch_tab may target an initial tab (registered via tabs/open),
            # but never an unknown tab.
            if self._tab_registry is not None and self._tab_registry.get(tab_id) is None:
                raise TabOwnershipError(
                    f"tab {tab_id} is not in the KaroX tab registry; the agent may only switch to tabs in this managed browser"
                )
            return self._call("switch_tab", {"tab_id": tab_id}, deadline_seconds)

    def close_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            tab_id = arguments.get("tab_id")
            if not isinstance(tab_id, str):
                raise BrowserError("tab_id must be a string")
            # B6: the agent may close ONLY tabs it created via new_tab in this
            # managed browser. Initial tabs (the managed browser's first tab,
            # or pre-existing tabs reported by the extension) are never closable
            # by the agent — this is the guard that prevents the B1 incident
            # where close_tab closed 8 of the user's personal Chrome tabs.
            if self._tab_registry is not None:
                rec = self._tab_registry.assert_closable(tab_id)
                if rec.is_initial_tab:
                    raise TabOwnershipError(
                        f"tab {tab_id} is the managed browser's initial tab and cannot be closed by the agent; "
                        "only tabs created via browser.new_tab may be closed"
                    )
            result = self._call("close_tab", {"tab_id": tab_id}, deadline_seconds)
            if self._tab_registry is not None:
                self._tab_registry.mark_closed(tab_id)
            return result

    def snapshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            result = self._call("snapshot", {}, deadline_seconds)
            return {
                "url": _safe_network_url(str(result.get("url", ""))),
                "title": str(redact(str(result.get("title", ""))))[:500],
                "viewport": result.get("viewport"),
                "focused_element": result.get("focused_element"),
                "tab_id": result.get("tab_id"),
                "context_id": self._context_id,
                "takeover_active": self._takeover_active,
                "context_preserved": True,
                "snapshot": redact(result.get("snapshot") or {}),
            }

    def click(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            selector = self._selector(arguments.get("selector"))
            self._assert_action_safe(self._inspect(selector, deadline_seconds))
            result = self._call("click", {"selector": selector}, deadline_seconds)
            return {"clicked": True, "selector": selector, **result}

    def fill(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            selector = self._selector(arguments.get("selector"))
            value = arguments.get("value")
            if not isinstance(value, str) or len(value) > 100_000:
                raise BrowserError("value must be a string up to 100000 characters")
            metadata = self._inspect(selector, deadline_seconds)
            descriptor = " ".join(
                str(metadata.get(key, ""))
                for key in ("type", "name", "id", "aria", "placeholder")
            )
            field_type = str(metadata.get("type", "")).lower()
            if (
                bool(metadata.get("secret"))
                or field_type == "password"
                or _SECRET_INPUT_HINT.search(descriptor)
            ):
                raise BrowserSecurityError(
                    "password, token, credential, or payment fields must be completed through user takeover or local credential injection"
                )
            if field_type == "email" and self.policy.allowed_emails:
                if value.strip().lower() not in self.policy.allowed_emails:
                    raise BrowserSecurityError("email is not allowed for this browser session")
            result = self._call("fill", {"selector": selector, "value": value}, deadline_seconds)
            return {"filled": True, "selector": selector, "value_length": len(value), **result}

    def fill_credential(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        """Fill a login field from local OS-keyring storage without exposing it."""

        with self._lock:
            self._assert_agent_input_allowed()
            selector = self._selector(arguments.get("selector"))
            reference = arguments.get("reference")
            field = arguments.get("field")
            if not isinstance(reference, str) or not reference.startswith("os-keyring:browser/"):
                raise BrowserError(
                    "browser credential reference must use os-keyring:browser/<name>"
                )
            if field not in {"username", "password"}:
                raise BrowserError("browser credential field must be username or password")
            if (
                self.policy.allowed_credential_refs
                and reference not in self.policy.allowed_credential_refs
            ):
                raise BrowserSecurityError(
                    "browser credential reference is not approved for this browser session"
                )

            metadata = self._inspect(selector, deadline_seconds)
            descriptor = " ".join(
                str(metadata.get(key, ""))
                for key in ("type", "name", "id", "aria", "placeholder")
            )
            field_type = str(metadata.get("type", "")).lower()
            if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "browser credential injection never fills payment, card, CVV/CVC, IBAN, or billing fields; use user takeover"
                )
            if field == "password":
                if field_type != "password" and not _SECRET_INPUT_HINT.search(descriptor):
                    raise BrowserSecurityError(
                        "password credential may only be injected into a password/credential field"
                    )
            else:
                if field_type == "password" or _SECRET_INPUT_HINT.search(descriptor):
                    raise BrowserSecurityError(
                        "username credential may not be injected into a password/secret field"
                    )
                if field_type not in {"email", "text", "input", "tel"}:
                    raise BrowserSecurityError(
                        "username credential target must be a text, email, or username-like input"
                    )

            secret = ""
            try:
                secret = self._credential_store.resolve_field(reference, field)
                # The profile-approved credential reference is the authority for
                # local username/email injection. ``allowed_emails`` remains a
                # separate guard only for model-supplied plain-text fill values.
                # The raw value crosses only the authenticated loopback bridge to
                # this KaroX-owned extension. It is never returned to the hosted
                # client, included in diagnostics, or written to persistent config.
                self._call(
                    "fill_secret",
                    {"selector": selector, "value": secret},
                    deadline_seconds,
                )
            except BrowserSecurityError:
                raise
            except Exception:
                # Extension/browser exceptions may echo submitted values; never
                # chain them across the hosted boundary.
                raise BrowserCredentialInjectionError(
                    "browser credential injection failed"
                ) from None
            finally:
                secret = ""

            return {
                "filled": True,
                "selector": selector,
                "field": field,
                "reference": reference,
                "masked": True,
            }

    def select(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            selector = self._selector(arguments.get("selector"))
            value = arguments.get("value")
            if not isinstance(value, (str, list)):
                raise BrowserError("select value must be a string or string array")
            return self._call("select", {"selector": selector, "value": value}, deadline_seconds)

    def press(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            selector = self._selector(arguments.get("selector"))
            key = arguments.get("key")
            if not isinstance(key, str) or len(key) > 100:
                raise BrowserError("key must be a string up to 100 characters")
            if key.lower() in {"enter", "numpadenter", "space"}:
                self._assert_action_safe(self._inspect(selector, deadline_seconds))
            return self._call("press", {"selector": selector, "key": key}, deadline_seconds)

    def wait_for(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            params = dict(arguments)
            params["timeout_ms"] = int(self._timeout(deadline_seconds) * 1000)
            return self._call("wait_for", params, deadline_seconds)

    def get_text(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            selector = self._selector(arguments.get("selector"))
            result = self._call("get_text", {"selector": selector}, deadline_seconds)
            text = str(result.get("text", ""))
            return {"text": str(redact(text[:30_000])), "truncated": len(text) > 30_000}

    def screenshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            name = arguments.get("name") or "screenshot"
            if not isinstance(name, str) or not name or len(name) > 128:
                raise BrowserError("screenshot name is invalid")
            result = self._call("screenshot", {}, deadline_seconds)
            data_url = result.get("data_url")
            prefix = "data:image/png;base64,"
            if not isinstance(data_url, str) or not data_url.startswith(prefix):
                raise BrowserError("extension screenshot did not return PNG data")
            try:
                png = base64.b64decode(data_url[len(prefix) :], validate=True)
            except Exception as exc:
                raise BrowserError("extension screenshot PNG is malformed") from exc
            record = self._artifacts.put(png, name=name, mime="image/png")
            return {
                "artifact_id": record.artifact_id,
                "name": record.name,
                "mime": record.mime,
                "size": record.size,
                "sha256": record.sha256,
                "full_page": False,
                "viewport_only": True,
                "tab_id": result.get("tab_id"),
            }

    def console(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            result = self._call("console", {}, deadline_seconds)
            return redact(result)

    def network_requests(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            if not self.policy.network_inspection:
                raise BrowserSecurityError("network inspection is not enabled for this browser session")
            result = self._call("network", {}, deadline_seconds)
            raw_rows = result.get("requests")
            rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
            url_contains = arguments.get("url_contains")
            method = arguments.get("method")
            resource_type = arguments.get("resource_type")
            filtered: list[dict[str, Any]] = []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                row["url"] = _safe_network_url(str(row.get("url", "")))
                row["error"] = str(redact(str(row.get("error", ""))))[:500] if row.get("error") else None
                if url_contains and str(url_contains).lower() not in row["url"].lower():
                    continue
                if method and str(row.get("method", "")).lower() != str(method).lower():
                    continue
                if resource_type and str(row.get("resource_type", "")).lower() != str(resource_type).lower():
                    continue
                filtered.append(row)
            return {"requests": filtered, "count": len(filtered), "filters_applied": True}

    def network_failures(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        result = self.network_requests({}, deadline_seconds)
        failed = [row for row in result["requests"] if int(row.get("status", 0) or 0) >= 400 or row.get("error")]
        return {"failed_requests": failed, "count": len(failed)}

    def request_user_takeover(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_started()
            reason = arguments.get("reason") or "sensitive browser action"
            if not isinstance(reason, str) or len(reason) > 500:
                raise BrowserError("takeover reason must be a string up to 500 characters")
            # Persist the pause before user control is granted. If the child
            # crashes after this point, the replacement manager restores the
            # takeover bit and keeps all agent input blocked until explicit resume.
            self._persist_takeover_active(True)
            self._takeover_active = True
            result = self._call("takeover", {}, deadline_seconds)
            if result.get("visual_takeover_failed") is True or result.get("agent_input_paused") is False:
                # Chrome explicitly proved that user control was NOT established.
                # Clear the durable pause only on that explicit negative ack; an
                # exception/ambiguous response remains fail-closed as takeover-active.
                self._persist_takeover_active(False)
                self._takeover_active = False
                return {
                    **result,
                    "takeover": False,
                    "agent_input_paused": False,
                    "session_id": self.policy.session_id,
                    "context_id": self._context_id,
                    "context_preserved": True,
                    "reason": str(redact(reason)),
                }
            return {
                **result,
                "takeover": True,
                "agent_input_paused": True,
                "session_id": self.policy.session_id,
                "context_id": self._context_id,
                "context_preserved": True,
                "reason": str(redact(reason)),
                "instruction": "Complete login, password, CAPTCHA, 2FA or consent in KaroX Browser, then resume the same session.",
            }

    def resume_after_user_takeover(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        with self._lock:
            if not self.takeover_active:
                raise BrowserSecurityError("browser session is not in user takeover mode")
            # A replacement MCP child may know takeover only from durable
            # ownership state. Reconnect first so the local flag is restored
            # before sending the explicit resume command to Chrome.
            self._ensure_started()
            if not self._takeover_active:
                raise BrowserSecurityError("browser takeover state could not be restored safely")
            result = self._call("resume", {}, deadline_seconds)
            # Clear the durable pause only after the browser acknowledged resume.
            # If persistence fails, leave the local bit true as well so agent
            # input stays fail-closed and the user can retry resume safely.
            self._persist_takeover_active(False)
            self._takeover_active = False
            return {
                **result,
                "resumed": True,
                "agent_input_paused": False,
                "session_id": self.policy.session_id,
                "context_id": self._context_id,
                "context_preserved": True,
                "recovered_blank": False,
            }

    def close(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.takeover_active and not force:
                raise BrowserSecurityError(
                    "browser cannot be closed while user takeover is active; resume the session first"
                )
            process = self._process
            bridge = self._bridge
            instance = self._instance
            was_open = bool(process is not None or bridge is not None)
            self._process = None
            self._bridge = None
            self._instance = None
            self._tab_registry = None
            self._takeover_active = False
            if process is not None:
                terminate_chrome_process(process)
            # B6: terminate only the owned process tree, never the user's
            # personal Chrome. When the process was launched by this manager,
            # terminate_chrome_process already handled it; for a reconnected
            # surviving managed browser we additionally kill the profile-scoped
            # processes via the per-instance user-data-dir.
            profile_stopped = False
            if instance is not None and process is None:
                profile_stopped = _terminate_profile_chrome(Path(instance.user_data_dir))
            if bridge is not None:
                bridge.stop()
            return {
                "closed": True,
                "was_open": was_open,
                "context_closed": was_open,
                "browser_stopped": process is not None or profile_stopped,
                "extension_bridge_stopped": bridge is not None,
                "profile_persistent": True,
                "session_id": self.policy.session_id,
            }

    def detach(self) -> dict[str, Any]:
        """Stop only the command bridge and leave dedicated Chrome alive."""
        with self._lock:
            bridge = self._bridge
            process = self._process
            was_open = bool(process is not None or bridge is not None)
            self._bridge = None
            self._process = None
            self._takeover_active = False
            if bridge is not None:
                bridge.stop()
            return {
                "closed": False,
                "detached": True,
                "was_open": was_open,
                "context_closed": False,
                "browser_stopped": False,
                "extension_bridge_stopped": bridge is not None,
                "profile_persistent": True,
                "process_was_owned": process is not None,
                "session_id": self.policy.session_id,
            }
