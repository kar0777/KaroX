"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional
from ..security import redact
from .errors import (
    BrowserCancelled,
    BrowserError,
    BrowserSecurityError,
)
from .netpolicy import (
    validate_browser_url,
)
from .redaction import (
    allowed_json_values,
    safe_console_text,
    safe_field_names,
    safe_network_url,
)
from .contract import (
    _MAX_NETWORK_ENTRIES,
    _MAX_CONSOLE_ENTRIES,
    _MAX_DIALOG_ENTRIES,
    _MAX_JSON_BYTES,
    _CANCEL_SLICE_SECONDS,
    _EVENT_PUMP_MS,
    CancelToken,
    _ConsoleEntry,
    _DialogEntry,
)


class BrowserCollectorsMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

    def _pump_driver_events(self) -> None:
        """Let Playwright dispatch events that arrived while the thread idled.

        Event handlers run only while this thread is inside a driver call. The
        response recorder reads a small JSON body from inside its handler, and
        that nested read completes on the next driver round trip -- so a
        network read straight after a click saw the request only once some
        later browser call happened to pump the connection. A short real wait
        is that round trip; a closed page simply has nothing to dispatch.
        """

        try:
            self._active_page().wait_for_timeout(_EVENT_PUMP_MS)
        except Exception:
            pass

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

    def _console(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        self._pump_driver_events()
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
        self._pump_driver_events()
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
        self._ensure_open()
        self._pump_driver_events()
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
