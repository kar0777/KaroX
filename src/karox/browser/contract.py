"""Shared browser-session contract: cancellation, records and action vocabulary.

Stateful Playwright behaviour lives in ``session.py`` and its mixins. This module
is the leaf they all may import without pulling in the session or the driver:
the cancellation token, type records, caps and the action→capability map.

It also breaks the historical ``guards → session`` edge: guards need only
:class:`CancelToken`, and importing this module gives them that without loading
the Playwright lifecycle.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import Capability
from .errors import BrowserCancelled
from .netpolicy import SessionPinnedProxy

DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800

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
#: One driver round trip before reading recorded console/network events.
_EVENT_PUMP_MS = 50
_IFRAME_READ_ATTEMPTS = 4
_IFRAME_RETRY_MS = 150
_INSTANCE_SCHEMA_VERSION = 1
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._\-]{1,128}")


def _windowless_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


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
    # tab id -> CDP session holding that tab paused between actions.
    parked: dict[str, Any] = field(default_factory=dict)


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
        "drag",
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
        "evaluate",
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
        # Renders the page's own three.js scene once more off-screen with flat
        # ID colours and restores it: a measurement, not page input.
        "census",
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
        "drag",
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
        # Runs the agent's own script in a page served from this machine; it
        # can change page state, so it is input, never a read.
        "evaluate",
    }
)

ACTION_CAPABILITIES: dict[str, Capability] = {
    **{action: Capability.BROWSER_READ for action in READ_ACTIONS},
    **{action: Capability.BROWSER_INPUT for action in INPUT_ACTIONS},
}

__all__ = [
    "ACTION_CAPABILITIES",
    "CancelToken",
    "DEFAULT_VIEWPORT_HEIGHT",
    "DEFAULT_VIEWPORT_WIDTH",
    "INPUT_ACTIONS",
    "READ_ACTIONS",
    "_AUTO_RECOVER_ACTIONS",
    "_windowless_flags",
    "_BrowserCommand",
    "_BrowserHandle",
    "_ConsoleEntry",
    "_CANCEL_SLICE_SECONDS",
    "_DialogEntry",
    "_DownloadEntry",
    "_EVENT_PUMP_MS",
    "_IFRAME_READ_ATTEMPTS",
    "_IFRAME_RETRY_MS",
    "_INSTANCE_SCHEMA_VERSION",
    "_ARTIFACT_NAME",
    "_MAX_CONSOLE_ENTRIES",
    "_MAX_DIALOG_ENTRIES",
    "_MAX_FIND_RESULTS",
    "_MAX_JSON_BYTES",
    "_MAX_NETWORK_ENTRIES",
    "_MAX_SELECTOR_LEN",
    "_MAX_SNAPSHOT_NODES",
    "_MAX_TEXT_CHARS",
    "_MAX_VALUE_LEN",
    "_PROFILE_ID",
]
