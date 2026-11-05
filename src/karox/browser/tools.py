"""Engine-neutral tool adapter for the browser session.

One stable surface, ``browser.command``, carries every action; adding an
action must never require a client reconnect. That matches the risk mapping
this release already ships (``risk_mapping.BROWSER_ACTION_RISK_KINDS`` grades
each call from its ``action`` payload) and the hosted tool allowlist naming.

The adapter is deliberately small and engine-neutral:

* :func:`browser_tool_definition` builds the ``ToolDefinition`` a karo-core
  runtime registers (capability ``browser.read`` on the descriptor; per-action
  authorization happens in the adapter, exactly like the hosted bridge did).
* :class:`BrowserToolAdapter` wraps one :class:`~karox.browser.session.BrowserSession`,
  enforces the per-action capability through an injected guard, honours a
  :class:`~karox.browser.session.CancelToken`, and returns *structured*
  results: ``{"ok": True, "action": ..., ...}`` or
  ``{"ok": False, "action": ..., "error_kind": ..., "error": ...}`` with the
  typed error vocabulary from :mod:`karox.browser.errors`.
* :class:`BrowserToolRuntime` keeps one session per KaroX session id for
  integrators that host several sessions in one process.
* :func:`execute_browser_command` is a drop-in replacement for the refusal in
  :mod:`karox.workspace_worker` with the identical call contract.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from ..artifacts import ArtifactStore
from ..core_types import InvalidCommand, ToolDefinition
from ..models import Capability
from ..static_server import LoopbackServer, RepositoryRequestHandler, page_url
from .credentials import BrowserCredentialStore
from .errors import (
    CANCELLED,
    PERMISSION_DENIED,
    BrowserCancelled,
    BrowserError,
    BrowserSecurityError,
    classify_browser_error,
)
from .netpolicy import BrowserAccessPolicy, is_local_browser_url
from .session import ACTION_CAPABILITIES, READ_ACTIONS, BrowserSession, CancelToken

__all__ = [
    "ACTION_CAPABILITIES",
    "BROWSER_ACTIONS",
    "BrowserToolAdapter",
    "BrowserToolRuntime",
    "CapabilityGuard",
    "CancelToken",
    "READ_ACTIONS",
    "browser_tool_definition",
    "execute_browser_command",
]

BROWSER_ACTIONS = frozenset(ACTION_CAPABILITIES) | frozenset({"serve"})

#: Actions served by the adapter itself, not the Playwright session. They are
#: still browser.command actions so the model has one stable surface and the
#: risk map grades them.
LOCAL_ACTIONS = frozenset({"serve"})

#: Called with the capability one action needs; raises PermissionError to deny.
CapabilityGuard = Callable[[Capability], None]

_TOOL_DESCRIPTION = (
    "Drive one session-owned browser: research pages and interact with them. "
    "Call with action plus a payload object. Navigation: open {url|page,width?,"
    "height?,profile?,detach?} — page is repository-relative and starts a "
    "loopback server for this repository, so a page you wrote can be clicked and "
    "read; serve {path?} returns that loopback URL without navigating. Back, "
    "forward, reload. Observation: tabs, snapshot "
    "(structured page map with refs k1..kN), snapshot_diff, find_text {text}, "
    "get_text {selector|ref?} (whole page when omitted), get_form_state "
    "{selector?}, page_info, iframe "
    "{selector}, console, network_failures, network_requests, cookies (names and "
    "scope only, never values), wait_for {selector?|ref?,state?|milliseconds?}, "
    "wait_navigation, wait_network_idle, evaluate {expression,timeout_ms?} (pages "
    "served from this machine only: a JS expression, arrow function or "
    "statements with return; promises are awaited and the JSON value comes "
    "back, e.g. window.app.state() -- read state this way instead of writing a "
    "probe page). Calls in one response run in order, so open, input and "
    "reads for one check belong in the same response. A headless page pauses "
    "after a few idle seconds between actions and resumes on the next one (its "
    "clock keeps running); use wait_for {milliseconds} when it must keep "
    "running. open and reload report render.frame_ms when frames are slow; "
    "on such a page clicks, presses and drags report render_after, the "
    "frames drawn in answer. "
    "Interaction: click {selector|ref,frame?}, "
    "dblclick, drag {selector|ref,dx,dy|to_selector|to_ref,x?,y?,steps?} (real "
    "mouse press-move-release: orbit, sliders, drag and drop), hover, focus, "
    "clear, type {selector|ref,text,delay_ms?}, fill "
    "{selector|ref,value}, select {selector|ref,value}, press {selector?|ref?,key}, "
    "set_checked {selector|ref,checked}, scroll {direction,amount?|selector}, "
    "dialog {mode?|decision?,prompt_text?}. Files: upload {selector|ref|trigger_"
    "selector,path|paths|artifact_id}, download_start {selector|ref|url}, "
    "download_status {download_id}. Screenshots: screenshot {full_page?,name?}, "
    "screenshot_region {selector|ref,name?}. Sessions: request_user_takeover "
    "{reason} and resume_after_user_takeover for CAPTCHA/2FA/payment/consent, "
    "recover (restore a blank or lost tab), reconnect {instance_id} (reattach a "
    "detached instance), close {user_confirmed:true,reason?}. Password fields "
    "refuse plain fills unless the session explicitly allows test credentials; "
    "prefer fill_credential {selector,reference,field} with an OS-keyring "
    "reference. Every result is structured; failures carry a typed error_kind."
)

_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "One browser action name, for example open or click.",
        },
        "payload": {
            "type": "object",
            "description": "Action arguments; see the tool description per action.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def browser_tool_definition(
    *,
    mutates: bool = False,
    replayable: bool = False,
) -> ToolDefinition:
    """The ``browser.command`` definition for a karo-core tool registry.

    ``mutates`` defaults to False because browser side effects are not
    repository mutations: a mutation lease cannot roll a click back, and the
    per-action risk gate (``risk_kind_for``) grades every call regardless. An
    integrator that wants durable idempotency for input actions registers the
    ``mutates=True`` variant and supplies leases and idempotency keys.
    ``replayable`` stays False in both cases: a snapshot describes a page that
    may have changed, so a stored answer must never be replayed as fresh.
    """

    return ToolDefinition(
        "browser.command",
        _TOOL_DESCRIPTION,
        Capability.BROWSER_READ,
        mutates,
        dict(_TOOL_SCHEMA),
        replayable=replayable,
    )


def _external_navigation(payload: Mapping[str, Any]) -> bool:
    for key in ("url",):
        value = payload.get(key)
        if isinstance(value, str) and value and not is_local_browser_url(value):
            return True
    return False


class BrowserToolAdapter:
    """Typed, cancellable, engine-neutral wrapper around one browser session."""

    def __init__(
        self,
        session: Any,
        *,
        capability_guard: Optional[CapabilityGuard] = None,
        serve_root: Optional[Path] = None,
    ) -> None:
        self.session = session
        self._capability_guard = capability_guard
        self._serve_root = Path(serve_root).resolve() if serve_root is not None else None
        self._httpd: Optional[LoopbackServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._serve_url: Optional[str] = None

    # -- authorization -------------------------------------------------------

    def _authorize(self, action: str, payload: Mapping[str, Any]) -> None:
        guard = self._capability_guard
        if guard is None:
            return
        if action in LOCAL_ACTIONS:
            # Serving the repository is a local read of bytes the agent may
            # already read; it must not inherit the browser.input risk of a
            # click or need the network capability.
            guard(Capability.BROWSER_READ)
            guard(Capability.REPO_READ)
            return
        guard(ACTION_CAPABILITIES[action])
        if action in {"open", "new_tab"} and _external_navigation(payload):
            # Leaving the machine is a second decision, never implied by
            # browser input on a dev server.
            guard(Capability.NETWORK)

    # -- local page serving --------------------------------------------------

    def _ensure_server(self) -> str:
        if self._httpd is not None and self._serve_url is not None:
            return self._serve_url
        root = self._serve_root
        if root is None or not root.is_dir():
            raise InvalidCommand(
                "local page serving is not configured for this session"
            )
        server = LoopbackServer(
            ("127.0.0.1", 0),
            lambda *args, **kwargs: RepositoryRequestHandler(
                *args, root=root, **kwargs
            ),
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="karox-browser-static",
            daemon=True,
        )
        thread.start()
        host, port = server.server_address[:2]
        self._httpd = server
        self._http_thread = thread
        self._serve_url = f"http://{host}:{port}/"
        return self._serve_url

    def _page_url(self, page: Any) -> str:
        root = self._serve_root
        if root is None:
            raise InvalidCommand("local page serving is not configured")
        try:
            return page_url(self._ensure_server(), root, page)
        except ValueError as exc:
            raise InvalidCommand(str(exc)) from exc

    def _open_payload(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if "page" not in payload:
            return dict(payload)
        if "url" in payload:
            raise InvalidCommand("open takes either url or page, not both")
        updated = dict(payload)
        page = updated.pop("page")
        updated["url"] = self._page_url(page)
        return updated

    def _serve(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        page = payload.get("path")
        if page is None:
            base = self._ensure_server()
            return {"url": base, "root": str(self._serve_root)}
        return {
            "url": self._page_url(page),
            "root": str(self._serve_root),
            "path": str(page),
        }

    def stop_server(self) -> None:
        server = self._httpd
        self._httpd = None
        self._http_thread = None
        self._serve_url = None
        if server is not None:
            server.shutdown()
            server.server_close()

    # -- execution -------------------------------------------------------------

    def execute(
        self,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken] = None,
    ) -> dict[str, Any]:
        """Run one browser.command call and return its structured envelope."""

        action = arguments.get("action")
        if not isinstance(action, str) or not action:
            raise InvalidCommand("browser.command action must be a non-empty string")
        payload = arguments.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise InvalidCommand("browser.command payload must be object")
        if action not in BROWSER_ACTIONS:
            raise InvalidCommand(f"unsupported browser.command action: {action}")
        if not isinstance(deadline_seconds, (int, float)) or deadline_seconds <= 0:
            raise InvalidCommand("browser.command deadline must be a positive number")
        if action == "open":
            payload = self._open_payload(payload)
        try:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if action == "serve":
                self._authorize(action, payload)
                result = self._serve(payload)
            else:
                self._authorize(action, payload)
                result = self.session.dispatch(action, payload, float(deadline_seconds), cancel)
        except BrowserCancelled:
            return {"ok": False, "action": action, "error_kind": CANCELLED,
                    "error": "browser call cancelled"}
        except BrowserSecurityError as exc:
            # A security error *is* a policy refusal; message classification
            # only overrides it when the refusal names a more actionable path
            # (hand over to the user, or the call was cancelled).
            kind = classify_browser_error(str(exc))
            if kind not in {PERMISSION_DENIED, "user_takeover_required", CANCELLED}:
                kind = PERMISSION_DENIED
            return {"ok": False, "action": action, "error_kind": kind, "error": str(exc)[:1000]}
        except BrowserError as exc:
            return {
                "ok": False,
                "action": action,
                "error_kind": classify_browser_error(str(exc)),
                "error": str(exc)[:1000],
            }
        except PermissionError as exc:
            return {"ok": False, "action": action, "error_kind": PERMISSION_DENIED,
                    "error": str(exc)[:1000]}
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a model
            message = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "action": action,
                "error_kind": classify_browser_error(message),
                "error": message[:1000],
            }
        if not isinstance(result, dict):
            result = {"result": result}
        if action == "close":
            self.stop_server()
        return {"ok": True, "action": action, **result}


    def core_handler(
        self, cancel: Optional[CancelToken] = None
    ) -> Callable[[Dict[str, Any], float], Dict[str, Any]]:
        """The ``(arguments, deadline_seconds) -> data`` handler CoreRuntime wants."""

        def handler(arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
            return self.execute(arguments, deadline_seconds, cancel)

        return handler


class BrowserToolRuntime:
    """One browser session per KaroX session id, for multi-session hosts."""

    def __init__(
        self,
        policy: BrowserAccessPolicy,
        *,
        artifacts_root: Optional[Path] = None,
        browser_root: Optional[Path] = None,
        upload_roots: tuple[Path, ...] = (),
        credential_store: Optional[BrowserCredentialStore] = None,
        capability_guard: Optional[CapabilityGuard] = None,
        serve_root: Optional[Path] = None,
    ) -> None:
        self._policy = policy
        self._artifacts_root = artifacts_root
        self._browser_root = browser_root
        self._upload_roots = tuple(upload_roots)
        self._credential_store = credential_store
        self._capability_guard = capability_guard
        self._serve_root = serve_root
        self._sessions: Dict[str, BrowserToolAdapter] = {}
        self._lock = threading.Lock()

    @property
    def policy(self) -> BrowserAccessPolicy:
        """The policy template every per-session adapter inherits.

        Exposed for diagnostics: a host that advertises browser tools should
        be able to state what the browser may do (downloads, takeover, domain
        allowlists) without opening a session first.
        """

        return self._policy

    def adapter(self, session_id: str) -> BrowserToolAdapter:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            artifacts_kwargs: dict[str, Any] = {}
            if self._artifacts_root is not None:
                artifacts_kwargs["root"] = self._artifacts_root
            artifacts = ArtifactStore(session_id, **artifacts_kwargs)
            policy = replace(self._policy, session_id=session_id)
            session = BrowserSession(
                artifacts,
                policy,
                self._credential_store,
                upload_roots=self._upload_roots,
                browser_root=self._browser_root,
            )
            adapter = BrowserToolAdapter(
                session,
                capability_guard=self._capability_guard,
                serve_root=self._serve_root,
            )
            self._sessions[session_id] = adapter
            return adapter

    def execute(
        self,
        session_id: str,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken] = None,
    ) -> dict[str, Any]:
        return self.adapter(session_id).execute(arguments, deadline_seconds, cancel)

    def close(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        """Tear one session's browser down and forget it."""

        with self._lock:
            adapter = self._sessions.pop(session_id, None)
        if adapter is None:
            return {"closed": True, "was_open": False, "session_id": session_id}
        session: BrowserSession = adapter.session
        if session.takeover_active and not force:
            self._sessions[session_id] = adapter
            raise BrowserSecurityError(
                "browser cannot be closed while user takeover is active; resume "
                "the session first or close with force"
            )
        try:
            if force:
                result = session.force_close()
            else:
                result = session.dispatch(
                    "close",
                    {"user_confirmed": True, "reason": "runtime close"},
                    30.0,
                    None,
                )
        except BrowserError:
            return {"closed": False, "was_open": session.is_open, "session_id": session_id}
        adapter.stop_server()
        return result

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id, force=True)


def execute_browser_command(
    runtime: Any,
    arguments: dict[str, Any],
    deadline_seconds: float,
    cancel: Optional[CancelToken] = None,
) -> dict[str, Any]:
    """Drop-in replacement for ``workspace_worker.execute_browser_command``.

    Same contract: ``(runtime, arguments, deadline_seconds) -> dict`` with the
    ``InvalidCommand`` refusal for malformed calls and ``app.*`` forwarded to
    :mod:`karox.desktop_apps`. The session comes from ``runtime._browser``,
    which may be a :class:`BrowserSession`, a :class:`BrowserToolAdapter`, or a
    :class:`BrowserToolRuntime` paired with ``runtime._browser_session_id``.
    Unlike v5, security failures return the structured envelope instead of
    raising, so the model sees the typed refusal and can recover.
    """

    action = arguments.get("action")
    payload = arguments.get("payload", {})
    if not isinstance(action, str) or not action:
        raise InvalidCommand("browser.command action must be a non-empty string")
    if not isinstance(payload, dict):
        raise InvalidCommand("browser.command payload must be object")
    if action.startswith("app."):
        from ..desktop_apps import execute_desktop_app_action

        return {
            "ok": True,
            "action": action,
            **execute_desktop_app_action(runtime, action, payload, deadline_seconds),
        }
    browser = getattr(runtime, "_browser", None)
    if browser is None:
        raise InvalidCommand(
            "browser.command is not available in this session; no browser runtime "
            "is attached"
        )
    if isinstance(browser, BrowserToolRuntime):
        session_id = getattr(runtime, "_browser_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise InvalidCommand(
                "browser runtime requires runtime._browser_session_id"
            )
        adapter = browser.adapter(session_id)
    elif isinstance(browser, BrowserToolAdapter):
        adapter = browser
    else:
        adapter = BrowserToolAdapter(browser)
    return adapter.execute(arguments, deadline_seconds, cancel)
