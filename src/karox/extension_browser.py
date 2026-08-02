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
    _PAYMENT_TEXT,
    _SECRET_INPUT_HINT,
    _safe_network_url,
    validate_browser_url,
)
from .browser_session import BrowserError, BrowserSecurityError
from .paths import runtime_dir
from .security import redact
from .system_chrome import chrome_profile_dir, find_system_chrome, terminate_chrome_process


class ExtensionBridgeError(BrowserError):
    """Raised when the local extension bridge or Chrome process fails."""


class _ExtensionBridgeServer:
    """One authenticated loopback WebSocket used by one Chrome profile."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
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
                        self._hello = {
                            "extension_version": str(message.get("extension_version", ""))[:80],
                            "browser": str(message.get("browser", ""))[:300],
                            "session_id": str(message.get("session_id", ""))[:200],
                        }
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
        return self._connected.is_set() and self._websocket is not None

    @property
    def hello(self) -> dict[str, Any]:
        return dict(self._hello)

    def wait_connected(self, timeout: float = 20.0) -> None:
        if not self._connected.wait(timeout=max(1.0, timeout)):
            raise ExtensionBridgeError(
                "KaroX Chrome extension did not connect; close any stale KaroX Chrome window and restart KaroX"
            )

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
        "newtab.html",
        "newtab.css",
        "newtab.js",
        "sidepanel.html",
        "sidepanel.css",
        "sidepanel.js",
    )


def _prepare_extension(server: _ExtensionBridgeServer) -> Path:
    destination = (runtime_dir() / "vnext" / "browser-extension-mv3").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    package_root = resources.files("karox.browser_extension")
    for name in _extension_source_files():
        source = package_root.joinpath(name)
        destination.joinpath(name).write_bytes(source.read_bytes())
    config = {
        "session_id": server.session_id,
        "websocket_url": server.websocket_url,
        "token": server.token,
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
    start_url = "chrome://newtab/" if installed_marker.is_file() else "chrome://extensions/"
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

    def __init__(self, artifacts: ArtifactStore, policy: BrowserAccessPolicy) -> None:
        if artifacts.session_id != policy.session_id:
            raise ValueError("browser policy and artifact store session IDs differ")
        if not policy.headed or not policy.user_takeover:
            raise ValueError("extension browser requires headed user-takeover mode")
        self._artifacts = artifacts
        self.policy = policy
        self._bridge: Optional[_ExtensionBridgeServer] = None
        self._process: Optional[subprocess.Popen[Any]] = None
        self._profile_dir: Optional[Path] = None
        self._context_id = f"ctx-ext-{uuid.uuid4().hex[:16]}"
        self._takeover_active = False
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        if self._bridge is None or not self._bridge.connected:
            return False
        # After a bridge restart the new manager attaches to the Chrome process
        # that survived the old bridge. It deliberately has no Popen handle for
        # that process, but the authenticated extension connection proves the
        # dedicated profile is alive and controllable.
        return self._process is None or self._process.poll() is None

    @property
    def takeover_active(self) -> bool:
        return self._takeover_active

    @property
    def context_id(self) -> str:
        return self._context_id

    @property
    def engine(self) -> str:
        return "chrome_extension_mv3"

    def _timeout(self, deadline_seconds: float, *, cap: float = 60.0) -> float:
        return max(1.0, min(float(deadline_seconds), cap))

    def _ensure_started(self, width: int = 1440, height: int = 900) -> None:
        if self.is_open:
            return
        if (
            self._process is not None
            and self._process.poll() is None
            and self._bridge is not None
        ):
            try:
                self._bridge.wait_connected(3.0)
            except ExtensionBridgeError as exc:
                raise ExtensionBridgeError(
                    "KaroX Browser needs one-time extension setup in the open Chrome window: "
                    "enable Developer mode, choose Load unpacked, and select the KaroX extension folder opened beside it"
                ) from exc
            marker = runtime_dir() / "vnext" / "browser-extension-mv3" / ".installed-in-karo-profile"
            marker.write_text("installed\n", encoding="utf-8")
            return
        if self._process is not None:
            terminate_chrome_process(self._process)
        if self._bridge is not None:
            self._bridge.stop()
        bridge = _ExtensionBridgeServer(self.policy.session_id)
        bridge.start()
        extension_dir = _prepare_extension(bridge)
        self._bridge = bridge
        self._profile_dir = chrome_profile_dir()
        # A surviving KaroX Chrome window reloads config.json on every reconnect.
        # Give it one reconnect interval before launching anything. This avoids
        # a visible close/reopen cycle and preserves all tabs across restarts.
        try:
            bridge.wait_connected(2.5)
        except ExtensionBridgeError:
            pass
        else:
            (extension_dir / ".installed-in-karo-profile").write_text(
                "installed\n", encoding="utf-8"
            )
            self._process = None
            return
        process, profile = _launch_extension_chrome(
            extension_dir,
            width=width,
            height=height,
        )
        self._process = process
        self._profile_dir = profile
        try:
            bridge.wait_connected(8.0)
        except Exception as exc:
            code = process.poll()
            if code is None:
                _show_extension_setup(extension_dir)
                raise ExtensionBridgeError(
                    "KaroX Browser needs one-time extension setup in the open Chrome window: "
                    "enable Developer mode, choose Load unpacked, and select the KaroX extension folder opened beside it"
                ) from exc
            bridge.stop()
            self._bridge = None
            self._process = None
            raise ExtensionBridgeError(
                f"Google Chrome exited before the KaroX extension connected (exit={code})"
            ) from exc
        (extension_dir / ".installed-in-karo-profile").write_text(
            "installed\n", encoding="utf-8"
        )

    def _call(self, method: str, params: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._ensure_started()
        assert self._bridge is not None
        return self._bridge.call(method, params, self._timeout(deadline_seconds))

    def _assert_agent_input_allowed(self) -> None:
        if self._takeover_active:
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
            return self._call("new_tab", params, deadline_seconds)

    def switch_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            return self._call("switch_tab", {"tab_id": arguments.get("tab_id")}, deadline_seconds)

    def close_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._assert_agent_input_allowed()
            return self._call("close_tab", {"tab_id": arguments.get("tab_id")}, deadline_seconds)

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
            descriptor = " ".join(str(item) for item in metadata.values())
            field_type = str(metadata.get("type", "")).lower()
            if field_type == "password" or _SECRET_INPUT_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "password, token, credential, or payment fields must be completed through user takeover"
                )
            if field_type == "email" and self.policy.allowed_emails:
                if value.strip().lower() not in self.policy.allowed_emails:
                    raise BrowserSecurityError("email is not allowed for this browser session")
            result = self._call("fill", {"selector": selector, "value": value}, deadline_seconds)
            return {"filled": True, "selector": selector, "value_length": len(value), **result}

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
            result = self._call("takeover", {}, deadline_seconds)
            self._takeover_active = True
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
            if not self._takeover_active:
                raise BrowserSecurityError("browser session is not in user takeover mode")
            result = self._call("resume", {}, deadline_seconds)
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
            if self._takeover_active and not force:
                raise BrowserSecurityError(
                    "browser cannot be closed while user takeover is active; resume the session first"
                )
            process = self._process
            bridge = self._bridge
            profile = self._profile_dir or chrome_profile_dir()
            was_open = bool(process is not None or bridge is not None)
            self._process = None
            self._bridge = None
            self._takeover_active = False
            terminate_chrome_process(process)
            profile_stopped = _terminate_profile_chrome(profile)
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
