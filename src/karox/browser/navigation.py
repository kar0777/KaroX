"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit
from ..security import redact
from .errors import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
)
from . import code_sources
from .netpolicy import (
    is_local_browser_url,
    same_navigation_target,
    validate_browser_url,
)
from .redaction import (
    safe_network_url,
)
from .contract import (
    CancelToken,
)


class BrowserNavigationMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

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
            if self._fulfill_code_source(route, request):
                return
            route.abort()
            return
        route.continue_()

    def _fulfill_code_source(self, route: Any, request: Any) -> bool:
        """Answer a local page's CDN import through KaroX; False to refuse it."""

        if not self.policy.code_sources:
            return False
        try:
            frame_url = str(request.frame.url)
        except Exception:
            frame_url = ""
        try:
            navigation = bool(request.is_navigation_request())
        except Exception:
            navigation = True
        url = code_sources.eligible(
            method=str(getattr(request, "method", "") or ""),
            url=str(getattr(request, "url", "") or ""),
            resource_type=str(getattr(request, "resource_type", "") or ""),
            navigation=navigation,
            frame_url=frame_url,
        )
        if url is None:
            return False
        response = code_sources.fetch(url)
        if response is None:
            return False
        try:
            route.fulfill(
                status=response.status,
                headers=code_sources.response_headers(response),
                body=response.body,
            )
        except Exception:
            return False
        return True

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
        # Playwright can deliver a late domcontentloaded/framenavigated event
        # while close() is tearing the handle down. A closed session has nowhere
        # to remember the URL; it is not an error for the callback to be late.
        handle = self._handle
        if handle is None:
            return None
        handle.last_safe_urls[tab_id] = validated
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

    @staticmethod
    def _resize(page: Any, width: int, height: int) -> None:
        current = getattr(page, "viewport_size", None) or {}
        if current.get("width") == width and current.get("height") == height:
            return
        try:
            page.set_viewport_size({"width": width, "height": height})
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            raise BrowserError(f"viewport could not be resized: {exc}") from None

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
        navigated: dict[str, Any] = {"navigated": operation, "url": self._safe_url(page)}
        self._attach_frame_report(page, navigated)
        return navigated

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
