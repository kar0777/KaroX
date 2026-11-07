"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

from typing import Any, Mapping, Optional
from ..security import redact
from .errors import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
)
from .netpolicy import (
    validate_browser_url,
)
from .contract import (
    _CANCEL_SLICE_SECONDS,
    CancelToken,
)


class BrowserWaitsMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

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
