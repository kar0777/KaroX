"""One stateful Playwright browser session owned by one KaroX session.

This is the engine the ``browser.command`` tool surface drives. It ports the
proven KaroX v5 ``browser_access`` session manager (owner-thread marshalling,
per-request URL validation, tab ownership, takeover, payment guard, bounded
redacted collectors) and completes the action surface the release risk
vocabulary already names: scrolling, hovering, typed keys, form state, text
search, snapshots with diffs, dialogs, iframes, uploads, downloads, cookie
persistence through named profiles, detached instances with ownership-proven
CDP reconnect, and stale-element recovery.

Security posture:

* Every URL is validated against :class:`BrowserAccessPolicy` before
  navigation *and* on every routed request, and again after navigation, so
  redirects and DNS rebinding are refused at the boundary they cross. A
  per-session authenticated loopback proxy performs the final upstream DNS
  lookup and pins the validated address.
* No tool result carries a cookie value, a password value, a storage blob, or
  an unbounded body. Text is size-capped and passed through the redaction
  filter; screenshots and downloads land in the session's artifact store and
  return references.
* Password-shaped fields refuse plain fills unless the policy explicitly
  enables ``plain_credentials`` (user-supplied test accounts); the value is
  then masked in the page before the fill and never echoed back. Keyring
  credential injection is always available through ``fill_credential``.
* CAPTCHA/2FA/payment/consent belong to the human: ``request_user_takeover``
  pauses every agent input until ``resume_after_user_takeover`` proves the
  page is back on an allowed URL.

Playwright's synchronous API is greenlet-based and every object it creates is
bound to the thread that started ``sync_playwright()``. Callers may arrive on
any thread, so every operation is marshalled to one permanent, session-owned
worker thread.
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from ..artifacts import ArtifactStore
from ..browser_bootstrap import BrowserBootstrapError, ensure_playwright_chromium
from ..models import Capability
from ..paths import runtime_dir
from ..process_identity import (
    capture_process_identity,
    process_is_running,
    read_process_create_time_ns,
)
from ..security import redact
from .credentials import (
    BrowserCredentialInjectionError,
    BrowserCredentialStore,
    inject_browser_credential,
)
from .errors import (
    BrowserCancelled,
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
    classify_browser_error,
)
from .netpolicy import (
    BrowserAccessPolicy,
    SessionPinnedProxy,
    is_local_browser_url,
    same_navigation_target,
    validate_browser_url,
)
from .guards import (
    _FREE_TRIAL_TEXT,
    _PAYMENT_TEXT,
    _STALE_MESSAGE,
    SessionGuardsMixin,
)
from .probes import (
    FIND_TEXT_PROBE,
    FORM_STATE_PROBE,
    SECRET_FIELD_HINT as _SECRET_INPUT_HINT,
    snapshot_probe,
)
from .redaction import (
    allowed_json_values,
    safe_console_text,
    safe_field_names,
    safe_network_url,
)

_MAX_SNAPSHOT_NODES = 500
_MAX_NETWORK_ENTRIES = 400
_MAX_CONSOLE_ENTRIES = 200
_MAX_DIALOG_ENTRIES = 50
_MAX_TEXT_CHARS = 20_000
_MAX_SELECTOR_LEN = 2_000
_MAX_VALUE_LEN = 100_000
_MAX_JSON_BYTES = 1_000_000
_MAX_FIND_RESULTS = 50
_CANCEL_SLICE_SECONDS = 0.4
_INSTANCE_SCHEMA_VERSION = 1
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._\-]{1,128}")

_PLAYWRIGHT_MISSING = (
    "browser automation requires the optional Playwright package and browser; "
    "install the 'browser' extra and run `python -m playwright install chromium`"
)

_PAYMENT_CREDENTIAL_HINT = re.compile(
    r"(?i)(card|cvv|cvc|iban|\bpan\b|billing|payment)"
)


class CancelToken:
    """Cooperative cancellation for one browser tool call.

    Playwright sync calls cannot be interrupted mid-flight, so cancellation is
    checked before dispatch and inside every wait loop, which runs in slices
    of at most ``_CANCEL_SLICE_SECONDS``. A cancelled call returns a typed
    ``cancelled`` structure; it never leaves the session half-mutated beyond
    what the in-flight driver call itself completed.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise BrowserCancelled("browser call cancelled")

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds; True when the token is cancelled."""

        return self._event.wait(timeout)

    def sleep(self, seconds: float) -> None:
        """Sleep in cancel-aware slices; returns early when cancelled."""

        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._event.wait(min(remaining, _CANCEL_SLICE_SECONDS)):
                return


@dataclass
class _ConsoleEntry:
    type: str
    text: str
    location: str
    tab_id: str


@dataclass
class _DialogEntry:
    type: str
    message: str
    resolved_with: Optional[str]
    tab_id: str


@dataclass
class _DownloadEntry:
    download_id: str
    status: str
    suggested_filename: str
    artifact_id: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None


@dataclass
class _BrowserHandle:
    playwright_ctx: Any
    browser: Any
    context: Any
    context_id: str = field(default_factory=lambda: f"ctx-{uuid.uuid4().hex[:16]}")
    proxy: Optional[SessionPinnedProxy] = None
    tabs: dict[str, Any] = field(default_factory=dict)
    active_tab_id: str = ""
    console: list[_ConsoleEntry] = field(default_factory=list)
    network: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[_DialogEntry] = field(default_factory=list)
    request_started: dict[int, float] = field(default_factory=dict)
    last_safe_urls: dict[str, str] = field(default_factory=dict)
    last_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    downloads: dict[str, _DownloadEntry] = field(default_factory=dict)
    pending_dialog: Any = None
    armed_dialog: Optional[dict[str, Any]] = None
    dialog_mode: str = "auto_dismiss"
    takeover_active: bool = False
    started_at: float = field(default_factory=time.time)
    engine: str = "playwright_chromium"
    profile: Optional[str] = None
    profile_dir: Optional[Path] = None
    detached: bool = False
    instance_id: Optional[str] = None
    browser_process: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
    cdp_browser: bool = False


@dataclass
class _BrowserCommand:
    callback: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: "concurrent.futures.Future[Any]"


# Actions that need a live active page; a late about:blank (hosted reconnect)
# is healed before the operation runs, never after it failed.
_AUTO_RECOVER_ACTIONS = frozenset(
    {
        "tabs",
        "snapshot",
        "snapshot_diff",
        "find_text",
        "get_text",
        "get_form_state",
        "click",
        "dblclick",
        "hover",
        "focus",
        "clear",
        "type",
        "fill",
        "fill_credential",
        "select",
        "press",
        "set_checked",
        "scroll",
        "wait_for",
        "screenshot",
        "screenshot_region",
        "iframe",
        "upload",
        "download_start",
        "request_user_takeover",
    }
)

READ_ACTIONS: frozenset[str] = frozenset(
    {
        "tabs",
        "snapshot",
        "snapshot_diff",
        "find_text",
        "get_text",
        "get_form_state",
        "wait_for",
        "wait_navigation",
        "wait_network_idle",
        "iframe",
        "screenshot",
        "screenshot_region",
        "console",
        "network_failures",
        "network_requests",
        "download_status",
        "cookies",
        "page_info",
        "recover",
        # Handing control to the human and taking it back is session-state
        # bookkeeping, not page input; the shared risk vocabulary grades both
        # as browser.read and the capability mapping must agree.
        "request_user_takeover",
        "resume_after_user_takeover",
    }
)

INPUT_ACTIONS: frozenset[str] = frozenset(
    {
        "open",
        "new_tab",
        "switch_tab",
        "close_tab",
        "click",
        "dblclick",
        "hover",
        "focus",
        "clear",
        "type",
        "fill",
        "fill_credential",
        "select",
        "press",
        "set_checked",
        "scroll",
        "dialog",
        "upload",
        "download_start",
        "back",
        "forward",
        "reload",
        "reconnect",
        "close",
    }
)

ACTION_CAPABILITIES: dict[str, Capability] = {
    **{action: Capability.BROWSER_READ for action in READ_ACTIONS},
    **{action: Capability.BROWSER_INPUT for action in INPUT_ACTIONS},
}


def _windowless_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class BrowserSession(SessionGuardsMixin):
    """Own one Playwright browser/context for exactly one KaroX session."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        policy: BrowserAccessPolicy,
        credential_store: Optional[BrowserCredentialStore] = None,
        *,
        upload_roots: Sequence[Path] = (),
        browser_root: Optional[Path] = None,
    ) -> None:
        if artifacts.session_id != policy.session_id:
            raise ValueError("browser policy and artifact store session IDs differ")
        self._artifacts = artifacts
        self.policy = policy
        self._credential_store = credential_store or BrowserCredentialStore()
        self._upload_roots = tuple(Path(root) for root in upload_roots)
        self._browser_root = (
            Path(browser_root)
            if browser_root is not None
            else runtime_dir() / "vnext" / "browser"
        )
        self._handle: Optional[_BrowserHandle] = None
        self._explicit_navigation_target: Optional[str] = None

        self._owner_queue: "queue.Queue[_BrowserCommand]" = queue.Queue()
        self._owner_ready = threading.Event()
        self._owner_thread_id: Optional[int] = None
        self._owner_thread = threading.Thread(
            target=self._owner_loop,
            name=f"karox-browser-owner-{policy.session_id[:24]}",
            daemon=True,
        )
        self._owner_thread.start()
        if not self._owner_ready.wait(timeout=5.0):
            raise BrowserSessionError("browser owner thread did not start")

    # -- owner-thread marshalling ------------------------------------------

    def _owner_loop(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._owner_ready.set()
        while True:
            command = self._owner_queue.get()
            try:
                if not command.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = command.callback(self, *command.args, **command.kwargs)
                except BaseException as exc:  # noqa: BLE001 - travels to caller
                    command.future.set_exception(exc)
                else:
                    command.future.set_result(result)
            finally:
                self._owner_queue.task_done()

    def _call_on_owner(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() == self._owner_thread_id:
            return callback(self, *args, **kwargs)
        if not self._owner_thread.is_alive():
            raise BrowserSessionError("browser owner thread is not running")
        future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
        self._owner_queue.put(
            _BrowserCommand(
                callback=callback,
                args=tuple(args),
                kwargs=dict(kwargs),
                future=future,
            )
        )
        return future.result()

    # -- session state ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def takeover_active(self) -> bool:
        return bool(self._handle and self._handle.takeover_active)

    @property
    def context_id(self) -> Optional[str]:
        """Stable identifier for the live context, safe to expose in diagnostics."""

        return self._handle.context_id if self._handle is not None else None

    def _ensure_open(self) -> _BrowserHandle:
        if self._handle is None:
            raise BrowserSessionError(
                "no browser session is open; call browser.command open first"
            )
        return self._handle

    def _active_page(self) -> Any:
        handle = self._ensure_open()
        page = handle.tabs.get(handle.active_tab_id)
        if page is None:
            raise BrowserSessionError("the active browser tab no longer exists")
        return page

    def _timeout_ms(self, deadline_seconds: float, *, default: float = 15.0) -> int:
        return max(1000, min(int(min(default, deadline_seconds) * 1000), 30_000))

    @staticmethod
    def _selector(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_SELECTOR_LEN:
            raise BrowserError("selector must be a 1-2000 character string")
        return value

    @staticmethod
    def _text(value: Any, *, name: str = "value") -> str:
        if not isinstance(value, str) or len(value) > _MAX_VALUE_LEN:
            raise BrowserError(f"{name} must be a string up to {_MAX_VALUE_LEN} characters")
        return value

    def _profile_dir(self, profile: str) -> Path:
        if not _PROFILE_ID.fullmatch(profile or ""):
            raise BrowserError(
                "browser profile must be a 1-64 character safe identifier"
            )
        return self._browser_root / "profiles" / profile

    def _instances_dir(self) -> Path:
        return self._browser_root / "instances"

    # -- dispatch ------------------------------------------------------------

    def dispatch(
        self,
        action: str,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken] = None,
    ) -> dict[str, Any]:
        """Run one guarded browser action and return its structured result."""

        if action not in ACTION_CAPABILITIES:
            raise BrowserError(f"unsupported browser action: {action}")
        # Class-level lookup, not getattr(self, ...): _call_on_owner rebinds the
        # function to the session on the owner thread, and a bound method would
        # receive ``self`` twice.
        method = getattr(type(self), "_" + action.replace(".", "_"), None)
        if method is None or not callable(method):
            raise BrowserError(f"unsupported browser action: {action}")
        return dict(self._call_on_owner(method, payload, deadline_seconds, cancel))

    @staticmethod
    def _guarded(
        method: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Heal a late about:blank and honour cancellation around one action."""

        @functools.wraps(method)
        def wrapped(
            self: "BrowserSession",
            payload: Mapping[str, Any],
            deadline_seconds: float,
            cancel: Optional[CancelToken] = None,
        ) -> Any:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if (
                method.__name__[1:] in _AUTO_RECOVER_ACTIONS
                and self.is_open
                and not self.takeover_active
            ):
                try:
                    self._recover_if_blank(deadline_seconds)
                except BrowserSecurityError:
                    raise
                except BrowserError:
                    pass
            result = method(self, payload, deadline_seconds, cancel)
            if cancel is not None:
                cancel.raise_if_cancelled()
            return result

        return wrapped

    # -- URL / page plumbing ---------------------------------------------------

    def _assert_agent_input_allowed(self) -> None:
        if self._ensure_open().takeover_active:
            raise BrowserSecurityError(
                "browser input is paused while the user has control; call "
                "resume_after_user_takeover after the user finishes"
            )

    def _route(self, route: Any) -> None:
        request = route.request
        try:
            target = validate_browser_url(request.url, self.policy)
            if is_local_browser_url(target) and not (
                self._explicit_navigation_target
                and same_navigation_target(target, self._explicit_navigation_target)
            ):
                sources: list[str] = []
                redirected = getattr(request, "redirected_from", None)
                seen = 0
                while redirected is not None and seen < 20:
                    sources.append(str(getattr(redirected, "url", "")))
                    redirected = getattr(redirected, "redirected_from", None)
                    seen += 1
                try:
                    frame_url = str(request.frame.url)
                except Exception:
                    frame_url = ""
                if frame_url:
                    sources.append(frame_url)
                if any(
                    urlsplit(source).scheme.lower() in {"http", "https"}
                    and not is_local_browser_url(source)
                    for source in sources
                ):
                    raise BrowserSecurityError(
                        "external pages cannot redirect or issue subrequests to localhost"
                    )
        except BrowserSecurityError:
            route.abort()
            return
        route.continue_()

    def _register_page(self, page: Any, *, make_active: bool = True) -> str:
        handle = self._ensure_open()
        for tab_id, existing in handle.tabs.items():
            if existing is page:
                if make_active:
                    handle.active_tab_id = tab_id
                return tab_id
        tab_id = f"tab-{uuid.uuid4().hex[:12]}"
        handle.tabs[tab_id] = page
        if make_active or not handle.active_tab_id:
            handle.active_tab_id = tab_id
        self._attach_page_collectors(page, tab_id)
        self._remember_safe_url(tab_id, page)

        def remember_navigation(frame: Any = None) -> None:
            try:
                if frame is not None and frame is not page.main_frame:
                    return
            except Exception:
                pass
            self._remember_safe_url(tab_id, page)

        def on_dialog(dialog: Any) -> None:
            self._handle_dialog(dialog, tab_id)

        try:
            page.on("framenavigated", remember_navigation)
            page.on("domcontentloaded", lambda: remember_navigation())
            if not self.policy.downloads:
                page.on("download", lambda download: download.cancel())
        except Exception:
            pass
        try:
            page.on("dialog", on_dialog)
        except Exception:
            pass
        page.on(
            "close",
            lambda _page: self._forget_tab(tab_id),
        )
        return tab_id

    def _forget_tab(self, tab_id: str) -> None:
        handle = self._handle
        if handle is None:
            return
        handle.tabs.pop(tab_id, None)
        handle.last_snapshots.pop(tab_id, None)
        if handle.active_tab_id == tab_id and handle.tabs:
            handle.active_tab_id = next(iter(handle.tabs))

    def _remember_safe_url(self, tab_id: str, page: Any) -> Optional[str]:
        try:
            value = str(page.url)
        except Exception:
            return None
        if not value.startswith(("http://", "https://")):
            return None
        try:
            validated = validate_browser_url(value, self.policy)
        except BrowserSecurityError:
            return None
        self._ensure_open().last_safe_urls[tab_id] = validated
        return validated

    @staticmethod
    def _safe_url(page: Any) -> str:
        try:
            return safe_network_url(str(page.url))
        except Exception:
            return ""

    def _navigate(self, page: Any, url: str, deadline_seconds: float) -> None:
        timeout = self._timeout_ms(deadline_seconds, default=30.0)
        self._explicit_navigation_target = url
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            validate_browser_url(str(page.url), self.policy)
            handle = self._ensure_open()
            for tab_id, candidate in handle.tabs.items():
                if candidate is page:
                    self._remember_safe_url(tab_id, page)
                    break
            self._save_profile_state()
        finally:
            self._explicit_navigation_target = None

    def _save_profile_state(self) -> None:
        """Persist cookies/localStorage of a named profile, values never leaving disk."""

        handle = self._handle
        if handle is None or not handle.profile or handle.detached:
            return
        assert handle.profile_dir is not None
        state_path = handle.profile_dir / "storage_state.json"
        try:
            handle.context.storage_state(path=str(state_path))
            try:
                os.chmod(state_path, 0o600)
            except OSError:
                pass
        except Exception:
            # A profile that cannot persist is a degraded convenience, never a
            # reason to fail the navigation the user asked for.
            pass

    def _recover_if_blank(self, deadline_seconds: float) -> dict[str, Any]:
        """Restore an unexpected about:blank without replacing the context."""

        handle = self._ensure_open()
        page = self._active_page()
        current = str(getattr(page, "url", "") or "")
        if current.startswith(("http://", "https://")):
            self._remember_safe_url(handle.active_tab_id, page)
            return {"recovered": False, "tab_id": handle.active_tab_id, "url": self._safe_url(page)}
        if current not in {"", "about:blank"}:
            raise BrowserSecurityError(
                "active browser tab is on an unsupported internal page"
            )
        target = handle.last_safe_urls.get(handle.active_tab_id)
        if not target:
            return {
                "recovered": False,
                "recoverable": False,
                "tab_id": handle.active_tab_id,
                "url": current or "about:blank",
            }
        self._navigate(page, target, deadline_seconds)
        return {
            "recovered": True,
            "tab_id": handle.active_tab_id,
            "url": self._safe_url(page),
        }

    # -- collectors -------------------------------------------------------------

    def _attach_page_collectors(self, page: Any, tab_id: str) -> None:
        handle = self._ensure_open()

        def on_console(msg: Any) -> None:
            if len(handle.console) >= _MAX_CONSOLE_ENTRIES:
                handle.console.pop(0)
            try:
                location = msg.location
                loc = (
                    f"{safe_network_url(str(location.get('url', '')))}"
                    f":{location.get('lineNumber', '')}"
                )
            except Exception:
                loc = ""
            handle.console.append(
                _ConsoleEntry(
                    type=str(getattr(msg, "type", "")).lower(),
                    text=safe_console_text(getattr(msg, "text", "")),
                    location=loc,
                    tab_id=tab_id,
                )
            )

        def on_request(request: Any) -> None:
            handle.request_started[id(request)] = time.monotonic()

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            started = (
                handle.request_started.pop(id(request), None) if request is not None else None
            )
            duration_ms = (
                round((time.monotonic() - started) * 1000, 2) if started else None
            )
            try:
                status = int(response.status)
            except Exception:
                status = 0
            try:
                headers = response.headers
                content_type = str(headers.get("content-type", ""))[:200]
                content_length = int(headers.get("content-length", "0") or 0)
            except Exception:
                content_type = ""
                content_length = 0
            entry: dict[str, Any] = {
                "url": safe_network_url(str(getattr(response, "url", ""))),
                "method": str(getattr(request, "method", ""))[:20],
                "status": status,
                "content_type": content_type,
                "resource_type": str(getattr(request, "resource_type", ""))[:50],
                "duration_ms": duration_ms,
                "tab_id": tab_id,
                "field_names": [],
                "values": {},
            }
            if (
                self.policy.network_inspection
                and "json" in content_type.lower()
                # Unknown/chunked response sizes are metadata-only. Reading an
                # unbounded body merely to discover its size would defeat the
                # inspection limit.
                and 0 < content_length <= _MAX_JSON_BYTES
            ):
                try:
                    raw = response.body()
                    if len(raw) <= _MAX_JSON_BYTES:
                        decoded = json.loads(raw.decode("utf-8"))
                        entry["field_names"] = safe_field_names(decoded)
                        entry["values"] = allowed_json_values(decoded)
                except Exception:
                    pass
            if len(handle.network) >= _MAX_NETWORK_ENTRIES:
                handle.network.pop(0)
            handle.network.append(entry)

        def on_request_failed(request: Any) -> None:
            started = handle.request_started.pop(id(request), None)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 2) if started else None
            )
            try:
                failure = str(request.failure or "request failed")
            except Exception:
                failure = "request failed"
            if len(handle.network) >= _MAX_NETWORK_ENTRIES:
                handle.network.pop(0)
            handle.network.append(
                {
                    "url": safe_network_url(str(getattr(request, "url", ""))),
                    "method": str(getattr(request, "method", ""))[:20],
                    "status": 0,
                    "content_type": "",
                    "resource_type": str(getattr(request, "resource_type", ""))[:50],
                    "duration_ms": duration_ms,
                    "tab_id": tab_id,
                    "error": str(redact(failure))[:500],
                    "field_names": [],
                    "values": {},
                }
            )

        page.on("console", on_console)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def _handle_dialog(self, dialog: Any, tab_id: str) -> None:
        """Apply the session dialog policy to one dialog, on the owner thread.

        Playwright invokes this from the driver event loop; touching the handle
        here is safe because the handle is only mutated on the owner thread and
        reads of small fields are atomic enough for bookkeeping.
        """

        handle = self._handle
        if handle is None:
            try:
                dialog.dismiss()
            except Exception:
                pass
            return
        try:
            message = safe_console_text(dialog.message)
            dialog_type = str(dialog.type)[:30]
        except Exception:
            message = ""
            dialog_type = "unknown"
        mode = handle.dialog_mode
        armed = handle.armed_dialog
        handle.armed_dialog = None
        if len(handle.dialogs) >= _MAX_DIALOG_ENTRIES:
            handle.dialogs.pop(0)
        if armed is not None:
            decision = str(armed.get("decision") or "dismiss")
            prompt_text = armed.get("prompt_text")
            try:
                if decision == "accept":
                    dialog.accept(str(prompt_text) if prompt_text is not None else "")
                else:
                    dialog.dismiss()
            except Exception:
                pass
            handle.dialogs.append(
                _DialogEntry(dialog_type, message, decision, tab_id)
            )
            handle.pending_dialog = None
            return
        if mode == "manual":
            handle.pending_dialog = dialog
            handle.dialogs.append(_DialogEntry(dialog_type, message, None, tab_id))
            return
        try:
            if mode == "auto_accept":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass
        handle.dialogs.append(
            _DialogEntry(dialog_type, message, "accept" if mode == "auto_accept" else "dismiss", tab_id)
        )

    # -- element resolution -----------------------------------------------------






    # -- lifecycle actions -------------------------------------------------------

    def _open(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        url = validate_browser_url(payload.get("url"), self.policy)
        width = int(payload.get("width", 1440))
        height = int(payload.get("height", 900))
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            raise BrowserError(
                "browser viewport is outside safe bounds (320-3840 x 240-2160)"
            )
        if self._handle is not None:
            self._assert_agent_input_allowed()
            page = self._active_page()
            self._navigate(page, url, deadline_seconds)
            handle = self._ensure_open()
            return {
                "open": True,
                "reused": True,
                "url": self._safe_url(page),
                "tab_id": handle.active_tab_id,
                "session_id": self.policy.session_id,
                "context_id": handle.context_id,
                "context_preserved": True,
                "engine": handle.engine,
                "profile": handle.profile,
                "detached": handle.detached,
            }
        profile = payload.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise BrowserError("profile must be a safe identifier string")
        detach = bool(payload.get("detach", False))
        if detach and not self.policy.allow_detach:
            raise BrowserSecurityError(
                "detached browser instances are not enabled for this session"
            )
        try:
            executable = ensure_playwright_chromium()
        except BrowserBootstrapError as exc:
            raise BrowserError(str(exc)) from None
        if detach:
            return self._open_detached(
                url,
                executable,
                profile=str(profile) if profile else f"detached-{uuid.uuid4().hex[:8]}",
                width=width,
                height=height,
                deadline_seconds=deadline_seconds,
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(_PLAYWRIGHT_MISSING) from exc
        ctx = sync_playwright().start()
        browser = None
        context = None
        proxy: Optional[SessionPinnedProxy] = None
        profile_dir: Optional[Path] = None
        try:
            proxy = SessionPinnedProxy(self.policy)
            proxy.start()
            browser = ctx.chromium.launch(
                headless=not self.policy.headed,
                proxy={
                    "server": proxy.server_url,
                    "username": proxy.username,
                    "password": proxy.password,
                },
            )
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": width, "height": height},
                "accept_downloads": bool(self.policy.downloads),
                "service_workers": "block",
            }
            if profile:
                profile_dir = self._profile_dir(str(profile))
                profile_dir.mkdir(parents=True, exist_ok=True)
                state_path = profile_dir / "storage_state.json"
                if state_path.is_file():
                    context_kwargs["storage_state"] = str(state_path)
            context = browser.new_context(**context_kwargs)
            context.route("**/*", self._route)
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=proxy,
                profile=str(profile) if profile else None,
                profile_dir=profile_dir,
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            page = context.new_page()
            tab_id = self._register_page(page, make_active=True)
            self._navigate(page, url, deadline_seconds)
        except Exception:
            self._handle = None
            self._teardown_quietly(context, browser, proxy, ctx, None)
            raise
        return {
            "open": True,
            "url": self._safe_url(page),
            "viewport": {"width": width, "height": height},
            "session_id": self.policy.session_id,
            "tab_id": tab_id,
            "context_id": handle.context_id,
            "headed": self.policy.headed,
            "context_isolated": True,
            "context_preserved": True,
            "engine": handle.engine,
            "profile": handle.profile,
            "detached": False,
        }

    def _open_detached(
        self,
        url: str,
        executable: Path,
        profile: str,
        *,
        width: int,
        height: int,
        deadline_seconds: float,
    ) -> dict[str, Any]:
        """Launch an ownership-proven detached Chromium and attach over CDP.

        The instance survives this KaroX process: the PID, its OS creation
        time, the loopback CDP port and the profile directory are persisted,
        and ``reconnect`` later proves the process identity before attaching.

        Detached instances are loopback-only. A bare Chromium command line
        cannot answer the pinning proxy's per-session authentication (v5 did
        that through its Chrome extension, which this line does not ship), so
        external HTTPS research stays on the managed session, which keeps the
        proxy and still persists cookies through named profiles.
        """

        if self.policy.external_https:
            raise BrowserSecurityError(
                "detached browser instances are loopback-only; use a managed "
                "session for external HTTPS research"
            )
        from playwright.sync_api import sync_playwright

        profile_dir = self._profile_dir(profile)
        userdata = profile_dir / "userdata"
        userdata.mkdir(parents=True, exist_ok=True)
        marker = userdata / "DevToolsActivePort"
        try:
            marker.unlink()
        except OSError:
            pass
        argv = [
            str(executable),
            f"--user-data-dir={userdata}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=TranslateUI",
            f"--window-size={width},{height}",
            "about:blank",
        ]
        if not self.policy.headed:
            argv.insert(1, "--headless=new")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, KaroX-owned binary
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_windowless_flags(),
            close_fds=True,
        )
        ctx = None
        port = 0
        ws_url = ""
        try:
            deadline = time.monotonic() + max(10.0, min(deadline_seconds, 60.0))
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BrowserError("detached browser exited during startup")
                if marker.is_file():
                    try:
                        lines = marker.read_text(encoding="ascii").splitlines()
                    except OSError:
                        lines = []
                    if len(lines) >= 2 and lines[0].strip().isdigit():
                        port = int(lines[0].strip())
                        ws_url = f"ws://127.0.0.1:{port}{lines[1].strip()}"
                        break
                time.sleep(0.1)
            if not ws_url:
                raise BrowserError("detached browser did not open a debugging port")
            ctx = sync_playwright().start()
            browser = ctx.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.set_default_timeout(30_000)
            instance_id = f"inst-{uuid.uuid4().hex[:16]}"
            identity = capture_process_identity(process.pid, owner=self.policy.session_id)
            record = {
                "schema": _INSTANCE_SCHEMA_VERSION,
                "instance_id": instance_id,
                "session_id": self.policy.session_id,
                "pid": process.pid,
                "create_time_ns": identity.create_time_ns,
                "port": port,
                "ws_url": ws_url,
                "profile": profile,
                "started_at": time.time(),
                "headed": self.policy.headed,
            }
            instances = self._instances_dir()
            instances.mkdir(parents=True, exist_ok=True)
            record_path = instances / f"{instance_id}.json"
            record_path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            try:
                os.chmod(record_path, 0o600)
            except OSError:
                pass
            context.route("**/*", self._route)
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=None,
                profile=profile,
                profile_dir=profile_dir,
                detached=True,
                instance_id=instance_id,
                browser_process=process,
                cdp_browser=True,
                engine="playwright_cdp_detached",
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            for existing_page in list(context.pages):
                self._register_page(existing_page, make_active=False)
            page = context.new_page()
            tab_id = self._register_page(page, make_active=True)
            self._navigate(page, url, deadline_seconds)
        except Exception:
            self._handle = None
            self._terminate_process(process)
            try:
                if ctx is not None:
                    ctx.stop()
            except Exception:
                pass
            raise
        return {
            "open": True,
            "detached": True,
            "instance_id": instance_id,
            "url": self._safe_url(page),
            "viewport": {"width": width, "height": height},
            "session_id": self.policy.session_id,
            "tab_id": tab_id,
            "context_id": handle.context_id,
            "headed": self.policy.headed,
            "engine": handle.engine,
            "profile": profile,
            "profile_persistent": True,
        }

    def _reconnect(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Attach to a detached instance this KaroX line launched, or fail closed."""

        if self._handle is not None:
            raise BrowserSessionError(
                "browser session is already open; close it before reconnecting"
            )
        if not self.policy.allow_detach:
            raise BrowserSecurityError(
                "detached browser instances are not enabled for this session"
            )
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not re.fullmatch(
            r"inst-[0-9a-f]{16}", instance_id
        ):
            raise BrowserError("instance_id must be a value returned by a detached open")
        record_path = self._instances_dir() / f"{instance_id}.json"
        if not record_path.is_file():
            raise BrowserSessionError(f"browser instance is not registered: {instance_id}")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrowserSessionError("browser instance record is unreadable") from exc
        pid = int(record.get("pid") or 0)
        create_time_ns = record.get("create_time_ns")
        ws_url = str(record.get("ws_url") or "")
        if not pid or not ws_url.startswith("ws://127.0.0.1:"):
            raise BrowserSessionError("browser instance record is malformed")
        if create_time_ns is None:
            # Fail closed: without a recorded creation time a recycled PID is
            # indistinguishable from the browser KaroX launched.
            raise BrowserSessionError(
                "browser instance identity was never proven; refusing to attach"
            )
        if not process_is_running(pid):
            raise BrowserSessionError(
                f"browser instance {instance_id} is no longer running"
            )
        live_create_time = read_process_create_time_ns(pid)
        if live_create_time is None or int(live_create_time) != int(create_time_ns):
            raise BrowserSessionError(
                "browser instance PID was reused by another process; refusing to attach"
            )
        from playwright.sync_api import sync_playwright

        ctx = sync_playwright().start()
        try:
            browser = ctx.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.route("**/*", self._route)
            profile = record.get("profile")
            profile_dir = self._profile_dir(str(profile)) if profile else None
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=None,
                profile=str(profile) if profile else None,
                profile_dir=profile_dir,
                detached=True,
                instance_id=instance_id,
                cdp_browser=True,
                engine="playwright_cdp_detached",
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            tab_ids = [
                self._register_page(existing, make_active=False)
                for existing in list(context.pages)
            ]
            if not handle.tabs:
                page = context.new_page()
                tab_ids = [self._register_page(page, make_active=True)]
            else:
                handle.active_tab_id = tab_ids[0]
        except Exception:
            self._handle = None
            try:
                ctx.stop()
            except Exception:
                pass
            raise
        return {
            "reconnected": True,
            "instance_id": instance_id,
            "pid": pid,
            "tab_ids": tab_ids,
            "active_tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "engine": handle.engine,
            "url": self._safe_url(self._active_page()),
        }

    @staticmethod
    def _terminate_process(process: Optional[subprocess.Popen]) -> None:  # type: ignore[type-arg]
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5.0)
            return
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass

    def _terminate_instance_pid(self, pid: int, create_time_ns: Optional[int]) -> bool:
        """Terminate a detached instance after re-proving ownership."""

        if not process_is_running(pid):
            return True
        if create_time_ns is not None:
            live = read_process_create_time_ns(pid)
            if live is None or int(live) != int(create_time_ns):
                return False
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=False,
                    creationflags=_windowless_flags(),
                )
                return True
            except Exception:
                return False
        try:
            os.kill(pid, 15)
            return True
        except OSError:
            return False

    def _teardown_quietly(
        self,
        context: Any,
        browser: Any,
        proxy: Optional[SessionPinnedProxy],
        ctx: Any,
        process: Optional[subprocess.Popen],  # type: ignore[type-arg]
    ) -> None:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        self._terminate_process(process)
        try:
            if proxy is not None:
                proxy.stop()
        except Exception:
            pass
        try:
            if ctx is not None:
                ctx.stop()
        except Exception:
            pass

    def _close(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        if payload.get("user_confirmed") is not True:
            raise BrowserSecurityError(
                "browser close requires explicit user confirmation and is never "
                "automatic cleanup"
            )
        reason = payload.get("reason", "")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise BrowserError("browser close reason must be a string up to 500 characters")
        handle = self._handle
        if handle is not None and handle.takeover_active:
            raise BrowserSecurityError(
                "browser cannot be closed while user takeover is active; resume "
                "the session first"
            )
        self._handle = None
        if handle is None:
            return {"closed": True, "was_open": False, "session_id": self.policy.session_id}
        self._save_profile_state_for(handle)
        instance_id = handle.instance_id
        keep_instance = bool(payload.get("keep_instance", False)) and handle.detached
        terminated = None
        if handle.detached and instance_id and not keep_instance:
            record_path = self._instances_dir() / f"{instance_id}.json"
            create_time_ns = None
            pid = 0
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                pid = int(record.get("pid") or 0)
                create_time_ns = record.get("create_time_ns")
            except Exception:
                pid = handle.browser_process.pid if handle.browser_process else 0
            if handle.browser_process is not None:
                self._terminate_process(handle.browser_process)
                terminated = True
            elif pid:
                terminated = self._terminate_instance_pid(pid, create_time_ns)
            try:
                record_path.unlink()
            except OSError:
                pass
        closed_context = False
        closed_browser = False
        proxy_stopped = False
        try:
            if not handle.cdp_browser:
                try:
                    handle.context.close()
                    closed_context = True
                except Exception:
                    pass
            try:
                handle.browser.close()
                closed_browser = True
            except Exception:
                pass
            try:
                if handle.proxy is not None:
                    handle.proxy.stop()
                    proxy_stopped = True
            except Exception:
                pass
        finally:
            try:
                handle.playwright_ctx.stop()
            except Exception:
                pass
        return {
            "closed": True,
            "was_open": True,
            "context_closed": closed_context or handle.cdp_browser,
            "browser_stopped": closed_browser,
            "dns_pinning_proxy_stopped": proxy_stopped,
            "instance_terminated": terminated,
            "instance_kept": keep_instance,
            "instance_id": instance_id,
            "profile_saved": bool(handle.profile),
            "session_id": self.policy.session_id,
        }

    def _save_profile_state_for(self, handle: _BrowserHandle) -> None:
        if not handle.profile or handle.detached:
            return
        assert handle.profile_dir is not None
        state_path = handle.profile_dir / "storage_state.json"
        try:
            handle.context.storage_state(path=str(state_path))
            try:
                os.chmod(state_path, 0o600)
            except OSError:
                pass
        except Exception:
            pass

    def force_close(self) -> dict[str, Any]:
        """Python-level teardown for the hosting runtime.

        Not a model-facing action: the ``close`` action stays refused while a
        user takeover is active, but the process that owns the session must
        always be able to tear its browser down on shutdown or revocation.
        """

        return dict(self._call_on_owner(type(self)._force_close_impl))

    def _force_close_impl(self) -> dict[str, Any]:
        handle = self._handle
        if handle is not None:
            handle.takeover_active = False
        return self._close({"user_confirmed": True, "reason": "runtime teardown"}, 30.0, None)

    # -- tab actions ---------------------------------------------------------------

    def _tabs(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        rows: list[dict[str, Any]] = []
        for tab_id, page in list(handle.tabs.items()):
            try:
                if page.is_closed():
                    handle.tabs.pop(tab_id, None)
                    continue
            except Exception:
                pass
            try:
                title = str(redact(page.title()))[:300]
            except Exception:
                title = ""
            rows.append(
                {
                    "tab_id": tab_id,
                    "active": tab_id == handle.active_tab_id,
                    "url": self._safe_url(page),
                    "title": title,
                }
            )
        return {
            "tabs": rows,
            "count": len(rows),
            "active_tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "takeover_active": handle.takeover_active,
            "engine": handle.engine,
            "detached": handle.detached,
            "profile": handle.profile,
        }

    def _new_tab(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        url = payload.get("url")
        page = handle.context.new_page()
        tab_id = self._register_page(page, make_active=True)
        if url is not None:
            self._navigate(page, validate_browser_url(url, self.policy), deadline_seconds)
        return {"created": True, "tab_id": tab_id, "url": self._safe_url(page)}

    def _switch_tab(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        tab_id = payload.get("tab_id")
        if not isinstance(tab_id, str) or tab_id not in handle.tabs:
            raise BrowserSecurityError("browser tab does not belong to this KaroX session")
        page = handle.tabs[tab_id]
        try:
            if page.is_closed():
                raise BrowserSessionError("browser tab is closed")
            page.bring_to_front()
        except BrowserSessionError:
            raise
        except Exception as exc:
            raise BrowserError("browser tab could not be activated") from exc
        handle.active_tab_id = tab_id
        return {"switched": True, "tab_id": tab_id, "url": self._safe_url(page)}

    def _close_tab(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        tab_id = payload.get("tab_id")
        if not isinstance(tab_id, str) or tab_id not in handle.tabs:
            raise BrowserSecurityError("browser tab does not belong to this KaroX session")
        if len(handle.tabs) <= 1:
            raise BrowserSecurityError("cannot close the last tab; use browser close")
        page = handle.tabs.pop(tab_id)
        page.close()
        if handle.active_tab_id == tab_id:
            handle.active_tab_id = next(iter(handle.tabs))
            handle.tabs[handle.active_tab_id].bring_to_front()
        return {"closed": True, "tab_id": tab_id, "active_tab_id": handle.active_tab_id}

    # -- navigation ------------------------------------------------------------------

    def _history(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        operation: str,
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        timeout = self._timeout_ms(deadline_seconds, default=30.0)
        if operation == "back":
            page.go_back(wait_until="domcontentloaded", timeout=timeout)
        elif operation == "forward":
            page.go_forward(wait_until="domcontentloaded", timeout=timeout)
        else:
            page.reload(wait_until="domcontentloaded", timeout=timeout)
        validate_browser_url(str(page.url), self.policy)
        handle = self._ensure_open()
        for tab_id, candidate in handle.tabs.items():
            if candidate is page:
                self._remember_safe_url(tab_id, page)
                break
        self._save_profile_state()
        return {"navigated": operation, "url": self._safe_url(page)}

    def _back(
        self, payload: Mapping[str, Any], deadline_seconds: float, cancel: Optional[CancelToken]
    ) -> dict[str, Any]:
        return self._history(payload, deadline_seconds, "back")

    def _forward(
        self, payload: Mapping[str, Any], deadline_seconds: float, cancel: Optional[CancelToken]
    ) -> dict[str, Any]:
        return self._history(payload, deadline_seconds, "forward")

    def _reload(
        self, payload: Mapping[str, Any], deadline_seconds: float, cancel: Optional[CancelToken]
    ) -> dict[str, Any]:
        return self._history(payload, deadline_seconds, "reload")

    # -- extraction ------------------------------------------------------------------

    _SNAPSHOT_PROBE = snapshot_probe(_MAX_SNAPSHOT_NODES)

    def _snapshot_data(self, page: Any) -> dict[str, Any]:
        try:
            data = page.evaluate(snapshot_probe(_MAX_SNAPSHOT_NODES))
        except Exception as exc:
            raise BrowserError("snapshot could not be captured") from exc
        return dict(data or {})

    def _snapshot(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        data = self._snapshot_data(page)
        try:
            title = str(redact(page.title()))
        except Exception:
            title = ""
        try:
            focused = page.evaluate(
                "() => document.activeElement ? document.activeElement.tagName.toLowerCase() : null"
            )
        except Exception:
            focused = None
        handle = self._ensure_open()
        handle.last_snapshots[handle.active_tab_id] = data
        return {
            "url": self._safe_url(page),
            "title": title,
            "viewport": page.viewport_size,
            "focused_element": focused,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "takeover_active": handle.takeover_active,
            "context_preserved": True,
            "snapshot": redact(data),
        }

    @staticmethod
    def _row_signature(category: str, row: Any) -> str:
        if not isinstance(row, Mapping):
            return f"{category}:{row}"
        parts = [category]
        for key in ("level", "text", "name", "label", "type", "href", "disabled", "protected"):
            if key in row:
                parts.append(f"{key}={row[key]}")
        return "|".join(str(part) for part in parts)

    def _snapshot_diff(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        tab_id = handle.active_tab_id
        previous = handle.last_snapshots.get(tab_id)
        current_result = self._snapshot(payload, deadline_seconds, cancel)
        current = handle.last_snapshots.get(tab_id) or {}
        if previous is None:
            return {
                "diffed": False,
                "reason": "no previous snapshot for this tab; snapshot stored",
                "url": current_result["url"],
            }
        diff: dict[str, Any] = {}
        for category in ("headings", "buttons", "inputs", "links", "dialogs", "tabs"):
            before = {
                self._row_signature(category, row)
                for row in previous.get(category, [])
            }
            after = {self._row_signature(category, row) for row in current.get(category, [])}
            added = sorted(after - before)[:20]
            removed = sorted(before - after)[:20]
            if added or removed:
                diff[category] = {"added": added, "removed": removed}
        return {
            "diffed": True,
            "url": current_result["url"],
            "changed": bool(diff),
            "diff": diff,
        }

    def _find_text(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        needle = self._text(payload.get("text", ""), name="text")
        if not needle.strip():
            raise BrowserError("find_text requires non-empty text")
        max_results = int(payload.get("max_results", 20))
        max_results = max(1, min(max_results, _MAX_FIND_RESULTS))
        page = self._active_page()
        probe = FIND_TEXT_PROBE
        try:
            matches = page.evaluate(probe, [needle, max_results])
        except Exception as exc:
            raise BrowserError("text search could not run on this page") from exc
        rows = [
            {**row, "text": str(redact(row.get("text", "")))[:300]}
            for row in (matches or [])
        ]
        return {"matches": rows, "count": len(rows), "url": self._safe_url(page)}

    def _get_text(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        locator = self._locator(payload, page)
        text = locator.inner_text(timeout=self._timeout_ms(deadline_seconds))
        clipped = text[:_MAX_TEXT_CHARS]
        return {"text": str(redact(clipped)), "truncated": len(text) > _MAX_TEXT_CHARS}

    def _page_info(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        handle = self._ensure_open()
        try:
            info = page.evaluate(
                """() => ({
                    title: document.title.slice(0, 300),
                    ready_state: document.readyState,
                    scroll: {x: window.scrollX, y: window.scrollY,
                             width: document.documentElement.scrollWidth,
                             height: document.documentElement.scrollHeight,
                             viewport_width: window.innerWidth,
                             viewport_height: window.innerHeight},
                    link_count: document.links.length,
                    form_count: document.forms.length
                })"""
            )
        except Exception as exc:
            raise BrowserError("page info could not be read") from exc
        return {
            "url": self._safe_url(page),
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "engine": handle.engine,
            **{str(k): redact(v) for k, v in (info or {}).items()},
        }

    def _get_form_state(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        selector = payload.get("selector")
        scope = self._selector(selector) if selector is not None else "body"
        probe = FORM_STATE_PROBE
        try:
            forms = page.evaluate(probe, scope)
        except Exception as exc:
            raise BrowserError("form state could not be read") from exc
        return {"forms": redact(forms or []), "url": self._safe_url(page)}

    def _iframe(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        selector = self._selector(payload.get("selector"))
        try:
            handle = page.locator(selector).first.element_handle(
                timeout=self._timeout_ms(deadline_seconds)
            )
        except Exception as exc:
            raise BrowserError(f"element_not_found: iframe {selector!r}") from exc
        frame = handle.content_frame() if handle is not None else None
        if frame is None:
            raise BrowserError("iframe target has no frame content")
        try:
            frame_url = str(frame.url)
            validate_browser_url(frame_url, self.policy)
        except BrowserSecurityError:
            raise
        except Exception:
            frame_url = ""
        try:
            data = frame.evaluate(snapshot_probe(_MAX_SNAPSHOT_NODES))
        except Exception as exc:
            raise BrowserError("iframe snapshot could not be captured") from exc
        return {
            "selector": selector,
            "url": safe_network_url(frame_url),
            "snapshot": redact(dict(data or {})),
        }

    # -- element input ----------------------------------------------------------------

    def _click(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        self._assert_action_safe(locator)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.click(timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {"clicked": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _dblclick(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.dblclick(timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {"dblclicked": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _hover(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.hover(timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {"hovered": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _focus(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        locator.focus(timeout=self._timeout_ms(deadline_seconds))
        return {"focused": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _clear(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._locator_metadata(locator)
        if metadata.get("secret") == "true" or metadata.get("type") == "password":
            raise BrowserSecurityError(
                "credential fields are cleared only through fill_credential or user takeover"
            )
        locator.fill("", timeout=self._timeout_ms(deadline_seconds))
        return {"cleared": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _guard_fill_target(self, locator: Any, value: str) -> dict[str, str]:
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "") for key in ("type", "name", "id", "aria", "placeholder")
        )
        secret_field = (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or bool(_SECRET_INPUT_HINT.search(descriptor))
        )
        if secret_field:
            if not self.policy.plain_credentials:
                raise BrowserSecurityError(
                    "password, token, credential, or payment fields must be completed "
                    "through user takeover or fill_credential; explicitly supplied test "
                    "credentials require the plain_credentials session permission"
                )
            if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "payment, card, CVV/CVC, IBAN, or billing fields are never filled "
                    "by the agent; use user takeover"
                )
        if metadata.get("type") == "email" and self.policy.allowed_emails:
            if value.strip().lower() not in self.policy.allowed_emails:
                raise BrowserSecurityError("email is not allowed for this browser session")
        return metadata

    def _fill(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        value = self._text(payload.get("value"))
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._guard_fill_target(locator, value)
        secret_field = (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or bool(
                _SECRET_INPUT_HINT.search(
                    " ".join(
                        metadata.get(key, "")
                        for key in ("type", "name", "id", "aria", "placeholder")
                    )
                )
            )
        )
        if secret_field:
            self._mask_secret_element(locator)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.fill(value, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "filled": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "value_length": len(value),
            "protected_field": secret_field,
        }

    def _type(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        text = self._text(payload.get("text", payload.get("value", "")), name="text")
        delay_ms = payload.get("delay_ms", 0)
        if not isinstance(delay_ms, (int, float)) or not 0 <= delay_ms <= 1000:
            raise BrowserError("delay_ms must be between 0 and 1000")
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._guard_fill_target(locator, text)
        secret_field = metadata.get("type") == "password" or metadata.get("secret") == "true"
        if secret_field:
            self._mask_secret_element(locator)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.press_sequentially(text, delay=float(delay_ms), timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "typed": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "chars": len(text),
            "protected_field": secret_field,
        }

    def _fill_credential(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Fill one login field from an opaque OS-keyring browser reference.

        The caller chooses only the target element, credential reference, and
        field name. The resolved value never enters tool arguments/results,
        logs, snapshots, or exception text.
        """

        self._assert_agent_input_allowed()
        selector = self._selector(payload.get("selector"))
        reference = payload.get("reference")
        field = payload.get("field")
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
        locator = self._active_page().locator(selector).first
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "credential input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "") for key in ("type", "name", "id", "aria", "placeholder")
        )
        field_type = metadata.get("type", "").lower()
        if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
            raise BrowserSecurityError(
                "browser credential injection never fills payment, card, CVV/CVC, "
                "IBAN, or billing fields; use user takeover"
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
        try:
            result = inject_browser_credential(
                locator=locator,
                credential_store=self._credential_store,
                reference=reference,
                field=field,
                timeout_ms=self._timeout_ms(deadline_seconds),
            )
        except BrowserCredentialInjectionError:
            raise
        return {"selector": selector, **result.to_dict()}

    def _select(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        raw = payload.get("value")
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            values = list(raw)
        else:
            raise BrowserError("select value must be a non-empty string or string array")
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.select_option(values, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "selected": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "count": len(values),
        }

    def _press(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        key = self._text(payload.get("key"), name="key")
        if not re.fullmatch(r"[A-Za-z0-9+]{1,32}", key):
            raise BrowserError("key must be a 1-32 character key name like Enter or Control+A")
        page = self._active_page()
        selector = payload.get("selector")
        timeout = self._timeout_ms(deadline_seconds)
        if selector is None and payload.get("ref") is None:
            if key.lower() in {"enter", "numpadenter", "space", "tab"}:
                self._assert_action_safe_page(page)
            page.keyboard.press(key)
            return {"pressed": True, "key": key, "target": "page"}
        locator = self._locator(payload, page)
        if key.lower() in {"enter", "numpadenter", "space"}:
            self._assert_action_safe(locator)

        def work() -> None:
            locator.press(key, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "pressed": True,
            "key": key,
            "target": str(payload.get("selector") or payload.get("ref")),
        }

    def _assert_action_safe_page(self, page: Any) -> None:
        """Payment guard for a page-level key press (no single element to inspect)."""

        try:
            data = page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el) return '';
                    const scope = el.closest('form,section,article,[role=dialog]') || document.body;
                    return ((el.innerText || el.value || '') + ' ' + (scope.innerText || '')).slice(0, 4000);
                }"""
            )
        except Exception:
            return
        text = str(data or "")
        if _PAYMENT_TEXT.search(text) and not _FREE_TRIAL_TEXT.search(text):
            if not self.policy.payment_confirmation:
                raise BrowserSecurityError(
                    "payment or subscription action requires explicit browser "
                    "payment_confirmation permission or user takeover"
                )

    def _set_checked(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        checked = payload.get("checked")
        if not isinstance(checked, bool):
            raise BrowserError("set_checked requires boolean checked")
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.set_checked(checked, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "set_checked": True,
            "checked": checked,
            "target": str(payload.get("selector") or payload.get("ref")),
        }

    def _scroll(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        direction = str(payload.get("direction", "down")).lower()
        if direction not in {"up", "down", "left", "right", "top", "bottom", "to_selector"}:
            raise BrowserError(
                "scroll direction must be up, down, left, right, top, bottom, or to_selector"
            )
        amount = payload.get("amount", 600)
        if not isinstance(amount, (int, float)) or not 1 <= amount <= 20_000:
            raise BrowserError("scroll amount must be between 1 and 20000 pixels")
        selector = payload.get("selector")
        if direction == "to_selector":
            if selector is None:
                raise BrowserError("to_selector requires a selector")
            locator = self._locator(payload, page)
            locator.scroll_into_view_if_needed(timeout=self._timeout_ms(deadline_seconds))
        else:
            script = """
            ([direction, amount]) => {
              const dx = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
              const dy = direction === 'up' ? -amount : direction === 'down' ? amount : 0;
              if (direction === 'top') window.scrollTo(0, 0);
              else if (direction === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);
              else window.scrollBy(dx, dy);
              return true;
            }
            """
            page.evaluate(script, [direction, float(amount)])
        state = page.evaluate(
            """() => ({x: window.scrollX, y: window.scrollY,
                       width: document.documentElement.scrollWidth,
                       height: document.documentElement.scrollHeight,
                       viewport_height: window.innerHeight})"""
        )
        return {"scrolled": direction, **{str(k): v for k, v in (state or {}).items()}}

    # -- waiting ----------------------------------------------------------------------

    def _wait_for(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        selector = payload.get("selector")
        state = payload.get("state", "visible")
        if state not in {"attached", "detached", "hidden", "visible"}:
            raise BrowserError("state must be attached, detached, hidden, or visible")
        total_ms = max(1000, min(int(deadline_seconds * 1000), 60_000))
        if selector is not None or payload.get("ref") is not None:
            locator = self._locator(payload, page)
            waited = 0
            slice_ms = int(_CANCEL_SLICE_SECONDS * 1000)
            while True:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                try:
                    locator.wait_for(state=state, timeout=slice_ms)
                    return {
                        "waited": True,
                        "target": str(payload.get("selector") or payload.get("ref")),
                        "state": state,
                    }
                except Exception:
                    waited += slice_ms
                    if waited >= total_ms:
                        raise BrowserError(
                            f"element_not_found: wait_for {state} timed out on target"
                        ) from None
        milliseconds = payload.get("milliseconds", 250)
        if not isinstance(milliseconds, (int, float)) or not 0 < milliseconds <= 10_000:
            raise BrowserError("milliseconds must be between 0 and 10000")
        if cancel is not None:
            cancel.sleep(milliseconds / 1000.0)
            cancel.raise_if_cancelled()
        else:
            page.wait_for_timeout(int(milliseconds))
        return {"waited": True, "milliseconds": int(milliseconds)}

    def _wait_load_state(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
        state: str,
    ) -> dict[str, Any]:
        page = self._active_page()
        total = max(0.5, min(float(deadline_seconds), 60.0))
        waited = 0.0
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            try:
                page.wait_for_load_state(state, timeout=int(_CANCEL_SLICE_SECONDS * 1000))
                return {"waited": state, "url": self._safe_url(page)}
            except Exception:
                waited += _CANCEL_SLICE_SECONDS
                if waited >= total:
                    raise BrowserError(f"navigation_timeout: {state} not reached") from None

    def _wait_navigation(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._wait_load_state(payload, deadline_seconds, cancel, "domcontentloaded")

    def _wait_network_idle(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._wait_load_state(payload, deadline_seconds, cancel, "networkidle")

    # -- takeover ----------------------------------------------------------------------

    def _request_user_takeover(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        if not self.policy.user_takeover:
            raise BrowserSecurityError(
                "user takeover is not enabled for this browser session"
            )
        if not self.policy.headed:
            raise BrowserSecurityError(
                "user takeover requires a visible headed browser"
            )
        reason = payload.get("reason") or "sensitive browser action"
        if not isinstance(reason, str) or len(reason) > 500:
            raise BrowserError("takeover reason must be a string up to 500 characters")
        handle.takeover_active = True
        page = self._active_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        return {
            "takeover": True,
            "agent_input_paused": True,
            "session_id": self.policy.session_id,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "context_preserved": True,
            "reason": str(redact(reason)),
            "instruction": (
                "Complete login, CAPTCHA, password, 2FA, consent, or payment review "
                "in the visible browser window. Then explicitly resume this same "
                "browser session."
            ),
        }

    def _resume_after_user_takeover(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        if not handle.takeover_active:
            raise BrowserSecurityError("browser session is not in user takeover mode")
        handle.takeover_active = False
        # The user may have closed the tab they were working in; fall back to
        # any surviving tab before declaring the resume failed.
        try:
            page = self._active_page()
            if page.is_closed():
                raise BrowserSessionError("closed")
        except BrowserSessionError:
            survivors = [
                tab_id
                for tab_id, candidate in handle.tabs.items()
                if not self._page_closed(candidate)
            ]
            if not survivors:
                raise BrowserSessionError(
                    "page_closed: every tab was closed during user takeover"
                ) from None
            handle.active_tab_id = survivors[0]
            page = handle.tabs[handle.active_tab_id]
        recovery = self._recover_if_blank(deadline_seconds)
        validate_browser_url(str(page.url), self.policy)
        self._save_profile_state()
        return {
            "resumed": True,
            "agent_input_paused": False,
            "session_id": self.policy.session_id,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "context_preserved": True,
            "recovered_blank": bool(recovery.get("recovered")),
            "url": self._safe_url(page),
        }

    @staticmethod
    def _page_closed(page: Any) -> bool:
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _recover(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        if self._page_closed(handle.tabs.get(handle.active_tab_id)):
            survivors = [
                tab_id
                for tab_id, candidate in handle.tabs.items()
                if not self._page_closed(candidate)
            ]
            if not survivors:
                page = handle.context.new_page()
                tab_id = self._register_page(page, make_active=True)
                result = self._recover_if_blank(deadline_seconds)
                return {"recovered": True, "created_tab": tab_id, **result}
            handle.active_tab_id = survivors[0]
        result = self._recover_if_blank(deadline_seconds)
        return {"recovered": bool(result.get("recovered")), **result}

    # -- evidence ----------------------------------------------------------------------

    def _screenshot(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._capture_screenshot(payload, deadline_seconds, region=False)

    def _screenshot_region(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        return self._capture_screenshot(payload, deadline_seconds, region=True)

    def _capture_screenshot(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        *,
        region: bool,
    ) -> dict[str, Any]:
        # Shared, *unwrapped* implementation: both public actions above are
        # wrapped for owner-thread/cancel/recovery handling, and a wrapped
        # method must never call another wrapped method with extra kwargs.
        page = self._active_page()
        full_page = bool(payload.get("full_page", not region))
        name = payload.get("name") or "screenshot"
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name):
            raise BrowserError(
                "name may contain 1-128 letters, digits, dots, hyphens, or underscores"
            )
        try:
            if region:
                locator = self._locator(payload, page)
                png = locator.screenshot(type="png", timeout=self._timeout_ms(deadline_seconds))
            else:
                png = page.screenshot(full_page=full_page, type="png")
        except Exception as exc:
            message = str(exc)
            if _STALE_MESSAGE.search(message):
                raise BrowserError(
                    "stale_element: screenshot target was replaced; re-snapshot and retry"
                ) from None
            raise BrowserError(f"screenshot_capture_failed: {redact(message)[:300]}") from None
        if not isinstance(png, (bytes, bytearray)) or not png:
            raise BrowserError("screenshot_capture_failed: no bytes were produced")
        record = self._artifacts.put(bytes(png), name=name, mime="image/png")
        viewport = page.viewport_size or {}
        return {
            "artifact_id": record.artifact_id,
            "name": record.name,
            "mime": record.mime,
            "size": record.size,
            "sha256": record.sha256,
            "width": viewport.get("width"),
            "height": viewport.get("height"),
            "full_page": full_page if not region else None,
            "region": region,
            "tab_id": self._ensure_open().active_tab_id,
        }

    def _console(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        tab_id = payload.get("tab_id")
        entries = [
            {
                "type": item.type,
                "text": item.text,
                "location": item.location,
                "tab_id": item.tab_id,
            }
            for item in handle.console
            if tab_id is None or item.tab_id == tab_id
        ]
        return {"entries": entries, "count": len(entries)}

    def _network_failures(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        entries = [
            item for item in handle.network if item.get("status", 0) >= 400 or item.get("error")
        ]
        return {"failed_requests": entries, "count": len(entries)}

    def _network_requests(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        if not self.policy.network_inspection:
            raise BrowserSecurityError(
                "network inspection is not enabled for this browser session"
            )
        url_contains = payload.get("url_contains")
        method = payload.get("method")
        resource_type = payload.get("resource_type")
        fields = payload.get("fields")
        if url_contains is not None and (
            not isinstance(url_contains, str) or len(url_contains) > 500
        ):
            raise BrowserError("url_contains must be a string up to 500 characters")
        if method is not None and not isinstance(method, str):
            raise BrowserError("method must be a string")
        if resource_type is not None and not isinstance(resource_type, str):
            raise BrowserError("resource_type must be a string")
        if fields is not None:
            if not isinstance(fields, list) or not all(
                isinstance(item, str) for item in fields
            ):
                raise BrowserError("fields must be a string array")
            requested_fields = {item.lower() for item in fields}
        else:
            requested_fields = {
                "model",
                "model_id",
                "provider",
                "usage",
                "credits",
                "plan",
                "trial",
                "subscription",
            }
        rows: list[dict[str, Any]] = []
        for item in self._ensure_open().network:
            if url_contains and url_contains.lower() not in str(item.get("url", "")).lower():
                continue
            if method and str(item.get("method", "")).lower() != method.lower():
                continue
            if resource_type and str(item.get("resource_type", "")).lower() != resource_type.lower():
                continue
            row = dict(item)
            raw_values = row.get("values")
            values: dict[Any, Any] = raw_values if isinstance(raw_values, dict) else {}
            row["values"] = {
                key: value for key, value in values.items() if key.lower() in requested_fields
            }
            rows.append(row)
        return {"requests": rows, "count": len(rows), "filters_applied": True}

    def _cookies(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Cookie *inventory* for session debugging: names and scope, never values."""

        handle = self._ensure_open()
        urls = payload.get("urls")
        if urls is not None:
            if not isinstance(urls, list) or not all(isinstance(item, str) for item in urls):
                raise BrowserError("urls must be a string array")
            raw = handle.context.cookies([validate_browser_url(item, self.policy) for item in urls])
        else:
            raw = handle.context.cookies()
        rows = [
            {
                "name": str(item.get("name", ""))[:200],
                "domain": str(item.get("domain", ""))[:200],
                "path": str(item.get("path", ""))[:200],
                "expires": item.get("expires"),
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
                "sameSite": item.get("sameSite"),
            }
            for item in (raw or [])[:200]
        ]
        return {
            "cookies": rows,
            "count": len(rows),
            "values_exposed": False,
            "profile": handle.profile,
        }

    def _dialog(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        mode = payload.get("mode")
        if mode is not None:
            if mode not in {"auto_dismiss", "auto_accept", "manual"}:
                raise BrowserError("dialog mode must be auto_dismiss, auto_accept, or manual")
            handle.dialog_mode = str(mode)
        decision = payload.get("decision")
        if decision is not None:
            if decision not in {"accept", "dismiss"}:
                raise BrowserError("dialog decision must be accept or dismiss")
            prompt_text = payload.get("prompt_text")
            if prompt_text is not None:
                prompt_text = self._text(prompt_text, name="prompt_text")[:10_000]
            if handle.pending_dialog is not None:
                dialog = handle.pending_dialog
                handle.pending_dialog = None
                try:
                    if decision == "accept":
                        dialog.accept(prompt_text if prompt_text is not None else "")
                    else:
                        dialog.dismiss()
                except Exception as exc:
                    raise BrowserError("dialog could not be resolved") from exc
                for entry in reversed(handle.dialogs):
                    if entry.resolved_with is None:
                        entry.resolved_with = str(decision)
                        break
                return {
                    "resolved": True,
                    "decision": decision,
                    "dialogs": [
                        {"type": item.type, "message": item.message, "resolved_with": item.resolved_with}
                        for item in handle.dialogs[-10:]
                    ],
                }
            # No dialog is pending: arm a one-shot decision for the next one and
            # wait for it inside the deadline. The wait pumps the Playwright
            # connection through a real driver call, otherwise a dialog event
            # (a slow alert from an earlier click) could never be dispatched
            # while this action owns the thread.
            handle.armed_dialog = {"decision": decision, "prompt_text": prompt_text}
            total = max(0.5, min(float(deadline_seconds), 60.0))
            waited = 0.0
            while waited < total:
                if cancel is not None and cancel.cancelled:
                    handle.armed_dialog = None
                    raise BrowserCancelled("browser call cancelled")
                try:
                    self._active_page().wait_for_timeout(
                        int(_CANCEL_SLICE_SECONDS * 1000)
                    )
                except Exception:
                    time.sleep(_CANCEL_SLICE_SECONDS)
                waited += _CANCEL_SLICE_SECONDS
                if handle.armed_dialog is None:
                    entry = handle.dialogs[-1] if handle.dialogs else None
                    return {
                        "resolved": True,
                        "decision": decision,
                        "dialog": {
                            "type": entry.type if entry else "",
                            "message": entry.message if entry else "",
                        },
                    }
            handle.armed_dialog = None
            raise BrowserError("no dialog appeared within the deadline")
        return {
            "mode": handle.dialog_mode,
            "pending": handle.pending_dialog is not None,
            "dialogs": [
                {
                    "type": item.type,
                    "message": item.message,
                    "resolved_with": item.resolved_with,
                    "tab_id": item.tab_id,
                }
                for item in handle.dialogs[-20:]
            ],
        }

    # -- file transfer -------------------------------------------------------------------

    def _resolve_upload_paths(self, payload: Mapping[str, Any]) -> list[Path]:
        artifact_id = payload.get("artifact_id")
        raw_paths = payload.get("path") or payload.get("paths")
        paths: list[Path] = []
        if artifact_id is not None:
            if not isinstance(artifact_id, str):
                raise BrowserError("artifact_id must be a string")
            _data, record = self._artifacts.read(artifact_id)
            blob = self._artifacts.root / f"{record.artifact_id}.bin"
            if not blob.is_file():
                raise BrowserError("artifact blob is no longer available")
            paths.append(blob)
        if raw_paths is not None:
            candidates = (
                [raw_paths] if isinstance(raw_paths, str) else list(raw_paths or [])
            )
            if not all(isinstance(item, str) for item in candidates):
                raise BrowserError("upload paths must be strings")
            if len(candidates) > 10:
                raise BrowserError("upload accepts at most 10 files")
            roots = self._upload_roots
            if not roots:
                raise BrowserSecurityError(
                    "upload is not available: no upload roots are configured for "
                    "this browser session"
                )
            for item in candidates:
                candidate = Path(item)
                resolved = (
                    candidate.resolve()
                    if candidate.is_absolute()
                    else (roots[0] / candidate).resolve()
                )
                if not any(
                    resolved == root.resolve()
                    or root.resolve() in resolved.parents
                    for root in roots
                ):
                    raise BrowserSecurityError(
                        "upload path escapes the configured upload roots"
                    )
                if not resolved.is_file():
                    raise BrowserError(f"upload path is not a file: {resolved.name}")
                paths.append(resolved)
        if not paths:
            raise BrowserError("upload requires path, paths, or artifact_id")
        return paths

    def _upload(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        paths = self._resolve_upload_paths(payload)
        page = self._active_page()
        timeout = self._timeout_ms(deadline_seconds)
        trigger = payload.get("trigger_selector")
        if trigger is not None:
            trigger_locator = page.locator(self._selector(trigger)).first
            try:
                with page.expect_file_chooser(timeout=timeout) as chooser_info:
                    trigger_locator.click(timeout=timeout)
                chooser = chooser_info.value
                chooser.set_files([str(item) for item in paths])
            except Exception as exc:
                raise BrowserError(
                    f"upload failed: {classify_browser_error(str(exc))}"
                ) from None
        else:
            locator = self._locator(payload, page)
            metadata = self._locator_metadata(locator)
            if metadata.get("type") not in {"file", "input"}:
                raise BrowserError(
                    "upload target must be an input[type=file] element or use "
                    "trigger_selector for a file-chooser button"
                )
            try:
                locator.set_input_files([str(item) for item in paths], timeout=timeout)
            except Exception as exc:
                raise BrowserError(
                    f"upload failed: {classify_browser_error(str(exc))}"
                ) from None
        return {
            "uploaded": True,
            "count": len(paths),
            "file_names": [item.name[:200] for item in paths],
            "target": str(payload.get("selector") or payload.get("ref") or trigger),
        }

    def _download_start(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        if not self.policy.downloads:
            raise BrowserSecurityError(
                "downloads are not enabled for this browser session"
            )
        page = self._active_page()
        timeout = self._timeout_ms(deadline_seconds, default=30.0)
        url = payload.get("url")
        selector = payload.get("selector")
        if url is None and selector is None:
            raise BrowserError("download_start requires url or selector")
        try:
            with page.expect_download(timeout=timeout) as download_info:
                if url is not None:
                    validated = validate_browser_url(url, self.policy)
                    try:
                        self._navigate(page, validated, deadline_seconds)
                    except Exception:
                        # A navigation that turns into a download is aborted by
                        # the browser; expect_download is the authority here.
                        pass
                else:
                    locator = self._locator(payload, page)
                    locator.click(timeout=timeout)
            download = download_info.value
        except BrowserSecurityError:
            raise
        except Exception as exc:
            raise BrowserError(
                f"download_failure: no download started ({classify_browser_error(str(exc))})"
            ) from None
        download_id = f"dl-{uuid.uuid4().hex[:12]}"
        suggested = str(download.suggested_filename or "download")[:200]
        handle = self._ensure_open()
        entry = _DownloadEntry(
            download_id=download_id,
            status="saving",
            suggested_filename=str(redact(suggested)),
        )
        handle.downloads[download_id] = entry
        try:
            tmp_path = self._browser_root / "downloads" / f"{download_id}.part"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(tmp_path))
            data = tmp_path.read_bytes()
            record = self._artifacts.put(
                data,
                name=re.sub(r"[^A-Za-z0-9._-]", "_", suggested)[:128] or "download.bin",
                mime="application/octet-stream",
            )
            try:
                tmp_path.unlink()
            except OSError:
                pass
            entry.status = "completed"
            entry.artifact_id = record.artifact_id
            entry.size = record.size
            entry.sha256 = record.sha256
        except Exception as exc:
            entry.status = "failed"
            entry.error = str(redact(str(exc)))[:300]
            raise BrowserError(f"download_failure: {entry.error}") from None
        if cancel is not None:
            cancel.raise_if_cancelled()
        return {
            "download_id": download_id,
            "status": entry.status,
            "suggested_filename": entry.suggested_filename,
            "artifact_id": entry.artifact_id,
            "size": entry.size,
            "sha256": entry.sha256,
        }

    def _download_status(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        download_id = payload.get("download_id")
        if not isinstance(download_id, str):
            raise BrowserError("download_status requires a download_id string")
        entry = handle.downloads.get(download_id)
        if entry is None:
            raise BrowserError(f"download_failure: unknown download_id {download_id[:40]}")
        return {
            "download_id": entry.download_id,
            "status": entry.status,
            "suggested_filename": entry.suggested_filename,
            "artifact_id": entry.artifact_id,
            "size": entry.size,
            "sha256": entry.sha256,
            "error": entry.error,
        }

    # Wrap every action exactly once, after the class body: keeps the methods
    # readable while guaranteeing Playwright never crosses threads, about:blank
    # is healed before page work, and cancellation brackets every call.


for _action_name in sorted(ACTION_CAPABILITIES):
    _method = getattr(BrowserSession, "_" + _action_name, None)
    if _method is None:
        continue
    setattr(BrowserSession, "_" + _action_name, BrowserSession._guarded(_method))
