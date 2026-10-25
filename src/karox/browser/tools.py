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
from ..core import InvalidCommand, ToolDefinition
from ..models import Capability
from .credentials import BrowserCredentialStore
from .errors import (
    BROWSER_ERROR,
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

BROWSER_ACTIONS = frozenset(ACTION_CAPABILITIES)

#: Called with the capability one action needs; raises PermissionError to deny.
CapabilityGuard = Callable[[Capability], None]

_TOOL_DESCRIPTION = (
    "Drive one session-owned browser: research pages and interact with them. "
    "Call with action plus a payload object. Navigation: open {url,width?,height?,"
    "profile?,detach?}, back, forward, reload. Observation: tabs, snapshot "
    "(structured page map with refs k1..kN), snapshot_diff, find_text {text}, "
    "get_text {selector|ref}, get_form_state {selector?}, page_info, iframe "
    "{selector}, console, network_failures, network_requests, cookies (names and "
    "scope only, never values), wait_for {selector?|ref?,state?|milliseconds?}, "
    "wait_navigation, wait_network_idle. Interaction: click {selector|ref,frame?}, "
    "dblclick, hover, focus, clear, type {selector|ref,text,delay_ms?}, fill "
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
    ) -> None:
        self.session = session
        self._capability_guard = capability_guard

    # -- authorization -------------------------------------------------------

    def _authorize(self, action: str, payload: Mapping[str, Any]) -> None:
        guard = self._capability_guard
        if guard is None:
            return
        guard(ACTION_CAPABILITIES[action])
        if action in {"open", "new_tab"} and _external_navigation(payload):
            # Leaving the machine is a second decision, never implied by
            # browser input on a dev server.
            guard(Capability.NETWORK)

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
        try:
            if cancel is not None:
                cancel.raise_if_cancelled()
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
        return {"ok": True, "action": action, **result}

    def execute_raising(
        self,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken] = None,
    ) -> dict[str, Any]:
        """v5-compatible variant: security/browser failures raise, not report."""

        result = self.execute(arguments, deadline_seconds, cancel)
        if result.get("ok"):
            return result
        kind = str(result.get("error_kind") or BROWSER_ERROR)
        message = str(result.get("error") or "browser call failed")
        if kind == CANCELLED:
            raise BrowserCancelled(message)
        if kind == PERMISSION_DENIED:
            raise BrowserSecurityError(message)
        raise BrowserError(f"{kind}: {message}")

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
    ) -> None:
        self._policy = policy
        self._artifacts_root = artifacts_root
        self._browser_root = browser_root
        self._upload_roots = tuple(upload_roots)
        self._credential_store = credential_store
        self._capability_guard = capability_guard
        self._sessions: Dict[str, BrowserToolAdapter] = {}
        self._lock = threading.Lock()

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
                session, capability_guard=self._capability_guard
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
                return session.force_close()
            return session.dispatch(
                "close",
                {"user_confirmed": True, "reason": "runtime close"},
                30.0,
                None,
            )
        except BrowserError:
            return {"closed": False, "was_open": session.is_open, "session_id": session_id}

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
    raising, so the model sees the typed refusal and can recover; callers that
    need the raising behavior use :meth:`BrowserToolAdapter.execute_raising`.
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
