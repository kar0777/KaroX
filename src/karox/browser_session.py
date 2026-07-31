"""A single stateful headless browser session owned by one KaroX bridge session.

ChatGPT (and Claude) reach the browser through one ``HostedToolsRuntime`` per
bridge process, and that runtime holds at most one live browser session.  The
session is bound to the KaroX ``session_id`` and torn down on ``browser.close``,
session revocation, or launcher exit.

Security posture (mirrors ``remote_tools._local_url``):

* URLs must resolve to ``127.0.0.1``/``::1``/``localhost`` and use ``http`` or
  ``https`` only.  ``file:``, ``data:``, and any non-local scheme are rejected
  before navigation, and a network route aborts any request that leaves
  localhost at any step (catches client-side redirects and asset fetches).
* There is no ``evaluate`` tool and no way to read cookies, localStorage, or
  sessionStorage.  The snapshot redacts the *values* of password-like inputs and
  never returns attributes that carry secrets.
* Every action has a timeout bounded by the call deadline, and snapshot/console
  output is size-capped and passed through the redaction filter.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from .artifacts import ArtifactStore
from .security import redact

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_BLOCKED_URL_SCHEMES = frozenset({"file", "data", "javascript", "blob", "about"})
# Computed styles worth reporting for nodes that look broken.  Kept short so a
# snapshot does not turn into a style dump.
_VISIBILITY_STYLES = (
    "display",
    "visibility",
    "opacity",
    "overflow",
    "pointer-events",
)
_MAX_SNAPSHOT_NODES = 500
_MAX_CONSOLE_ENTRIES = 200
_MAX_TEXT_CHARS = 20_000
_MAX_SELECTOR_LEN = 2000
_MAX_VALUE_LEN = 100_000
# Playwright lazy-imported so a bridge without the ``browser`` extra still loads.
_PLAYWRIGHT_MISSING = (
    "browser automation requires the optional Playwright package and browser; "
    "install the 'browser' extra and run `python -m playwright install chromium`"
)


class BrowserError(RuntimeError):
    pass


class BrowserSecurityError(BrowserError):
    pass


class BrowserSessionError(BrowserError):
    pass


def _validate_local_url(value: Any) -> str:
    """Accept only http(s) URLs on localhost, with no embedded credentials.

    DNS is resolved before navigation so a hostname that *looks* local but points
    elsewhere (a poisoned hosts entry, a public DNS record for ``localhost``) is
    refused.  ``::1`` is allowed without resolution since ``socket`` already
    treats it as loopback.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BrowserSecurityError("url must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise BrowserSecurityError("only http and https URLs are allowed")
    if parsed.scheme in _BLOCKED_URL_SCHEMES or parsed.scheme not in {"http", "https"}:
        raise BrowserSecurityError("blocked URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserSecurityError("URLs with embedded credentials are not allowed")
    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        raise BrowserSecurityError("browser tools are limited to localhost URLs")
    # Confirm the name actually resolves to loopback, not a public record.
    if host not in {"::1"}:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise BrowserSecurityError("localhost URL did not resolve") from exc
        for info in infos:
            addr = info[4][0]
            if not (addr == "127.0.0.1" or addr == "::1" or addr == "localhost"):
                raise BrowserSecurityError(
                    "localhost URL resolved to a non-loopback address"
                )
    return value


def _validate_viewport(arguments: Mapping[str, Any]) -> tuple[int, int]:
    width = int(arguments.get("width", 1440))
    height = int(arguments.get("height", 900))
    if not 320 <= width <= 3840 or not 240 <= height <= 2160:
        raise BrowserError("browser viewport is outside safe bounds (320-3840 x 240-2160)")
    return width, height


def _validate_selector(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SELECTOR_LEN:
        raise BrowserError("selector must be a 1-2000 character string")
    return value


def _validate_text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > _MAX_VALUE_LEN:
        raise BrowserError("value must be a string up to 100000 characters")
    return value


@dataclass
class _ConsoleEntry:
    type: str
    text: str
    location: str


@dataclass
class _BrowserHandle:
    playwright_ctx: Any
    browser: Any
    page: Any
    console: list = field(default_factory=list)
    network: list = field(default_factory=list)
    started_at: float = field(default_factory=time.time)


class BrowserSessionManager:
    """Owns the lifecycle of one headless Chromium for a KaroX session."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts
        self._handle: Optional[_BrowserHandle] = None

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def _ensure_open(self) -> _BrowserHandle:
        if self._handle is None:
            raise BrowserSessionError("no browser session is open; call karox.browser.open first")
        return self._handle

    def _route_local_only(self, page: Any) -> None:
        def route_local(route: Any) -> None:
            try:
                target = urlsplit(route.request.url)
            except Exception:
                route.abort()
                return
            if target.scheme in _BLOCKED_URL_SCHEMES:
                route.abort()
                return
            if target.scheme not in {"http", "https"}:
                route.abort()
                return
            if (target.hostname or "").lower() not in _LOCAL_HOSTS:
                route.abort()
                return
            route.continue_()

        page.route("**/*", route_local)

    def _attach_collectors(self, handle: _BrowserHandle) -> None:
        page = handle.page

        def on_console(msg: Any) -> None:
            if len(handle.console) >= _MAX_CONSOLE_ENTRIES:
                handle.console.pop(0)
            try:
                location = msg.location
                loc_str = f"{location.get('url', '')}:{location.get('lineNumber', '')}"
            except Exception:
                loc_str = ""
            handle.console.append(
                _ConsoleEntry(
                    type=str(getattr(msg, "type", "")).lower(),
                    text=str(getattr(msg, "text", "")),
                    location=loc_str,
                )
            )

        def on_response(response: Any) -> None:
            try:
                status = int(response.status)
            except Exception:
                return
            if status < 400:
                return
            if len(handle.network) >= _MAX_CONSOLE_ENTRIES:
                handle.network.pop(0)
            try:
                url = str(response.url)
            except Exception:
                url = ""
            handle.network.append(
                {
                    "url": url,
                    "status": status,
                    "method": str(getattr(response, "request", None) and response.request.method or ""),
                }
            )

        page.on("console", on_console)
        page.on("response", on_response)

    def open(
        self,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
    ) -> dict[str, Any]:
        url = _validate_local_url(arguments.get("url"))
        width, height = _validate_viewport(arguments)
        if self._handle is not None:
            # Idempotent re-open: reuse the existing page rather than leaking a
            # second browser.  Navigate to the new URL on the same context.
            self._handle.console.clear()
            self._handle.network.clear()
            page = self._handle.page
            self._navigate(page, url, deadline_seconds)
            return {
                "open": True,
                "url": self._safe_url(),
                "reused": True,
            }
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(_PLAYWRIGHT_MISSING) from exc
        ctx = sync_playwright().start()
        try:
            browser = ctx.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            ctx.stop()
            raise
        handle = _BrowserHandle(playwright_ctx=ctx, browser=browser, page=page)
        self._attach_collectors(handle)
        self._route_local_only(page)
        self._handle = handle
        self._navigate(page, url, deadline_seconds)
        return {
            "open": True,
            "url": self._safe_url(),
            "viewport": {"width": width, "height": height},
            "session_id": self._artifacts.session_id,
        }

    def _navigate(self, page: Any, url: str, deadline_seconds: float) -> None:
        timeout = max(1000, min(30_000, int(deadline_seconds * 1000)))
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        final = page.url
        # Re-validate the final URL: a localhost page that redirected to an
        # external origin through JS must be caught here even though the route
        # aborted the request -- the address bar may still report it.
        _validate_local_url(final)

    def _safe_url(self) -> str:
        handle = self._ensure_open()
        try:
            return str(redact(handle.page.url))
        except Exception:
            return ""

    def close(self) -> dict[str, Any]:
        handle = self._handle
        self._handle = None
        if handle is None:
            return {"closed": True, "was_open": False}
        closed_browser = False
        try:
            try:
                handle.browser.close()
                closed_browser = True
            except Exception:
                pass
        finally:
            try:
                handle.playwright_ctx.stop()
            except Exception:
                pass
        return {"closed": True, "was_open": True, "browser_stopped": closed_browser}

    # -- actions -------------------------------------------------------------

    def _timeout_ms(self, deadline_seconds: float, *, action_default: float = 15.0) -> int:
        return max(1000, min(int(min(action_default, deadline_seconds) * 1000), 30_000))

    def click(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = _validate_selector(arguments.get("selector"))
        handle.page.locator(selector).first.click(timeout=self._timeout_ms(deadline_seconds))
        return {"clicked": True, "selector": selector}

    def fill(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = _validate_selector(arguments.get("selector"))
        value = _validate_text(arguments.get("value"))
        handle.page.locator(selector).first.fill(value, timeout=self._timeout_ms(deadline_seconds))
        return {"filled": True, "selector": selector}

    def select(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = _validate_selector(arguments.get("selector"))
        raw_value = arguments.get("value")
        if not isinstance(raw_value, (str, list)) or not raw_value:
            raise BrowserError("select value must be a non-empty string or array")
        if isinstance(raw_value, list):
            values = [str(item) for item in raw_value]
        else:
            values = [str(raw_value)]
        handle.page.locator(selector).first.select_option(values, timeout=self._timeout_ms(deadline_seconds))
        return {"selected": True, "selector": selector, "values": values}

    def press(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = _validate_selector(arguments.get("selector"))
        key = _validate_text(arguments.get("key"))
        handle.page.locator(selector).first.press(key, timeout=self._timeout_ms(deadline_seconds))
        return {"pressed": True, "selector": selector, "key": key}

    def wait_for(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = arguments.get("selector")
        state = arguments.get("state", "visible")
        if state not in {"attached", "detached", "hidden", "visible"}:
            raise BrowserError("state must be attached, detached, hidden, or visible")
        timeout = max(1000, min(int(deadline_seconds * 1000), 60_000))
        if selector is not None:
            _validate_selector(selector)
            handle.page.locator(selector).first.wait_for(state=state, timeout=timeout)
            return {"waited": True, "selector": selector, "state": state}
        milliseconds = arguments.get("milliseconds", 250)
        if not isinstance(milliseconds, (int, float)) or not 0 < milliseconds <= 10000:
            raise BrowserError("milliseconds must be between 0 and 10000")
        handle.page.wait_for_timeout(int(milliseconds))
        return {"waited": True, "milliseconds": int(milliseconds)}

    def get_text(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        selector = _validate_selector(arguments.get("selector"))
        text = handle.page.locator(selector).first.inner_text(timeout=self._timeout_ms(deadline_seconds))
        clipped = text[:_MAX_TEXT_CHARS]
        return {"text": str(redact(clipped)), "truncated": len(text) > _MAX_TEXT_CHARS}

    def screenshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        full_page = bool(arguments.get("full_page", True))
        name = arguments.get("name") or "screenshot"
        if not isinstance(name, str) or not name or len(name) > 128:
            raise BrowserError("name must be a 1-128 character string")
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", name):
            raise BrowserError("name may only contain letters, digits, dots, hyphens, and underscores")
        png_bytes = handle.page.screenshot(full_page=full_page, type="png")
        if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
            raise BrowserError("screenshot produced no bytes")
        record = self._artifacts.put(bytes(png_bytes), name=name, mime="image/png")
        return {
            "artifact_id": record.artifact_id,
            "name": record.name,
            "mime": record.mime,
            "size": record.size,
            "sha256": record.sha256,
            "width": handle.page.viewport_size.get("width") if handle.page.viewport_size else None,
            "height": handle.page.viewport_size.get("height") if handle.page.viewport_size else None,
            "full_page": full_page,
        }

    def console(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        entries = [
            {"type": e.type, "text": str(redact(e.text)), "location": e.location}
            for e in handle.console
        ]
        return {"entries": entries, "count": len(entries)}

    def network_failures(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        entries = [
            {
                "url": str(redact(item["url"])),
                "status": item["status"],
                "method": item["method"],
            }
            for item in handle.network
        ]
        return {"failed_requests": entries, "count": len(entries)}

    def snapshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        """Return a structured, secret-free description of the current page.

        GLM and other text-only models cannot see a PNG, so this is the primary
        representation of the interface.  Password values are never read; a
        secret-like input is reported only with its label, type, and disabled
        state.
        """
        handle = self._ensure_open()
        page = handle.page
        # The snapshot probe runs against the live DOM.  It only reads node
        # shape, roles, labels, and computed visibility -- never values of
        # secret-shaped inputs.
        probe = r"""
        (maxNodes) => {
          const SECRET_TYPE = new Set(['password', 'hidden', 'search']);
          const SECRET_HINT = /password|secret|token|api[_-]?key|credential|cookie/i;
          const result = {nodes: [], headings: [], buttons: [], inputs: [], links: [],
                         dialogs: [], tabs: [], text: '', issues: []};
          const visibleText = [];
          const body = document.body;
          if (!body) return result;
          const walker = document.createTreeWalker(body, NodeFilter.SHOW_ELEMENT);
          let count = 0;
          while (walker.nextNode() && count < maxNodes) {
            count++;
            const el = walker.currentNode;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const tag = el.tagName.toLowerCase();
            const inViewport = rect.bottom > 0 && rect.right > 0 &&
              rect.top < (window.innerHeight || 0) && rect.left < (window.innerWidth || 0);
            const hasText = el.childNodes && Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
            const text = (el.textContent || '').trim().slice(0, 200);
            if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
              result.headings.push({level: tag, text: text});
            }
            if (tag === 'button' || el.getAttribute('role') === 'button') {
              result.buttons.push({name: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0,200),
                                   disabled: el.disabled || el.getAttribute('aria-disabled') === 'true'});
            }
            if (tag === 'a' && el.href) {
              result.links.push({text: text.slice(0,120), href: (el.getAttribute('href') || '').slice(0,300)});
            }
            if (['input','select','textarea'].includes(tag)) {
              const inputType = (el.getAttribute('type') || tag).toLowerCase();
              const labelFor = document.querySelector('label[for=\"' + (el.id || '') + '\"]');
              const secret = inputType === 'password' || SECRET_HINT.test(el.name || '') ||
                SECRET_HINT.test(el.id || '') || SECRET_HINT.test(el.getAttribute('aria-label') || '') ||
                SECRET_HINT.test(labelFor ? labelFor.textContent : '');
              result.inputs.push({
                label: (labelFor ? labelFor.textContent.trim().slice(0,120) : (el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0,120)),
                type: inputType,
                disabled: el.disabled,
                secret: !!secret,
                // values of secret inputs are never included; non-secret value is masked to length only
                value_length: secret ? null : (el.value || '').length,
                checked: inputType === 'checkbox' || inputType === 'radio' ? !!el.checked : null,
              });
            }
            if (el.getAttribute('role') === 'dialog' || tag === 'dialog') {
              result.dialogs.push({name: (el.getAttribute('aria-label') || text).slice(0,200), open: el.open || el.getAttribute('open') !== null});
            }
            if (el.getAttribute('role') === 'tab' || el.getAttribute('role') === 'tablist') {
              result.tabs.push({text: text.slice(0,120), selected: el.getAttribute('aria-selected') === 'true'});
            }
            if (style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity) !== 0) {
              if (text) visibleText.push(text);
            }
            // issue probes
            if (hasText && rect.height === 0 && style.display !== 'none') {
              result.issues.push({kind: 'zero_height_text', selector: tag, text: text.slice(0,80),
                                  display: style.display, visibility: style.visibility});
            }
            if (rect.right > (window.innerWidth || 0) + 1) {
              result.issues.push({kind: 'horizontal_overflow', selector: tag, right: Math.round(rect.right),
                                  viewport_width: window.innerWidth || 0});
            }
            if (!inViewport && rect.width > 0 && rect.height > 0 && style.position !== 'fixed') {
              result.issues.push({kind: 'off_viewport', selector: tag,
                                  display: style.display, visibility: style.visibility});
            }
          }
          result.text = visibleText.join(' ').slice(0, 20000);
          result.scroll = {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
                           viewport_width: window.innerWidth, viewport_height: window.innerHeight,
                           horizontal_overflow: document.documentElement.scrollWidth > (window.innerWidth || 0) + 1};
          return result;
        }
        """
        try:
            data = page.evaluate(probe, _MAX_SNAPSHOT_NODES)
        except Exception as exc:
            raise BrowserError("snapshot could not be captured") from exc
        url = self._safe_url()
        try:
            title = str(redact(page.title()))
        except Exception:
            title = ""
        try:
            active = page.evaluate("() => { const a = document.activeElement; return a ? (a.tagName||'').toLowerCase() : null; }")
        except Exception:
            active = None
        try:
            viewport = page.viewport_size
        except Exception:
            viewport = None
        # Redact the whole snapshot structure: text and link hrefs may carry a
        # token a page echoed back.  Values of secret inputs are already absent.
        safe = redact(data)
        return {
            "url": url,
            "title": title,
            "viewport": viewport,
            "focused_element": active,
            "snapshot": safe,
        }
