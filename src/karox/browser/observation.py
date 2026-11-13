"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional
from ..security import redact
from . import census
from .errors import (
    BrowserCancelled,
    BrowserError,
    BrowserSecurityError,
)
from .netpolicy import (
    validate_browser_url,
)
from .probes import (
    FIND_TEXT_PROBE,
    FORM_STATE_PROBE,
    snapshot_probe,
)
from .redaction import (
    safe_network_url,
)
from .pageperf import (
    FRAME_PROBE_JS,
    HAS_CANVAS_JS,
    INPUT_TIMEOUT_NOTE,
    MAX_EXPRESSION_CHARS,
    MOTION_READ_JS,
    MOTION_RECORDER_JS,
    MOVING_TIMEOUT_NOTE,
    RESPONSE_FRAME_PROBE_JS,
    SLOW_FRAME_MS,
    SLOW_INPUT_MS,
    body_source,
    bounded_value,
    evaluate_timeout_ms,
    expression_source,
    frame_probe_enabled,
    idle_after_slow_report,
    is_syntax_error,
    response_frame_report,
    slow_frame_report,
    slow_input_note,
    target_moved,
    unstable_target_timeout,
)
from .contract import (
    _MAX_SNAPSHOT_NODES,
    _MAX_TEXT_CHARS,
    _MAX_FIND_RESULTS,
    _IFRAME_READ_ATTEMPTS,
    _IFRAME_RETRY_MS,
    CancelToken,
)


def _frame_page_key(page: Any) -> str:
    """The page a frame measurement belongs to: its URL without query or hash."""

    url = str(getattr(page, "url", "") or "")
    return url.split("#", 1)[0].split("?", 1)[0]


class BrowserObservationMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

    def _attach_frame_report(self, page: Any, result: dict[str, Any]) -> None:
        """Measure frame time on a local page with a canvas; note it when slow.

        Never raises and never delays an ordinary page: the probe runs only
        where a canvas exists, and on a responsive page it takes ~0.1 s.
        """

        self._last_frame_ms = 0
        if not frame_probe_enabled():
            return
        try:
            if not self._is_local_page_url(str(getattr(page, "url", "") or "")):
                return
            if not page.evaluate(HAS_CANVAS_JS):
                return
            data = page.evaluate(FRAME_PROBE_JS)
        except Exception:
            return
        try:
            self._last_frame_ms = int((data or {}).get("frame_ms") or 0)
        except (TypeError, ValueError, AttributeError):
            self._last_frame_ms = 0
        key = _frame_page_key(page)
        report = slow_frame_report(data)
        if report is not None:
            result["render"] = report
            self._note_slow_frames(key, int(report["frame_ms"]))
            return
        # A fast probe after slow ones may only mean the page stopped drawing
        # while idle; say so once, with the cost it had.
        earlier = self._slow_frame_pending().pop(key, None)
        if earlier:
            result["render"] = idle_after_slow_report(self._last_frame_ms, earlier)

    def _slow_frame_pending(self) -> dict[str, int]:
        """Pages whose last measured frames were slow, by URL without query."""

        return self.__dict__.setdefault("_slow_frames_pending", {})

    def _note_slow_frames(self, key: str, frame_ms: int) -> None:
        self._slow_frame_pending()[key] = int(frame_ms)
        self.__dict__.setdefault("_slow_frame_pages", set()).add(key)

    def _attach_response_frames(self, action: str, result: dict[str, Any], slow: bool) -> None:
        """Measure the frames a local canvas page draws in answer to an input.

        Runs only after a slow input or on a page that drew slow frames before,
        so an ordinary page pays nothing and a responsive game keeps its timing.
        """

        if not frame_probe_enabled():
            return
        try:
            if self._page_script_may_block():
                return
            page = self._active_page()
            key = _frame_page_key(page)
            if not slow and key not in self.__dict__.get("_slow_frame_pages", set()):
                return
            if not self._is_local_page_url(str(getattr(page, "url", "") or "")):
                return
            if not page.evaluate(HAS_CANVAS_JS):
                return
            data = page.evaluate(RESPONSE_FRAME_PROBE_JS)
        except Exception:  # noqa: BLE001 - diagnosis only; the input already ran
            return
        report = response_frame_report(data, action)
        if report is None:
            return
        result["render_after"] = report
        self._note_slow_frames(key, int(report["frame_ms"]))
        self._last_frame_ms = max(int(getattr(self, "_last_frame_ms", 0) or 0), int(report["frame_ms"]))

    def _input_timing(
        self,
        action: str,
        started: float,
        result: dict[str, Any],
        recording: bool = False,
    ) -> dict[str, Any]:
        elapsed = time.monotonic() - started
        motion = None
        slow = elapsed * 1000 >= SLOW_INPUT_MS
        if recording and slow:
            motion = self._read_motion()
        note = slow_input_note(action, elapsed, motion)
        if note is not None:
            result.update(note)
        # A target that kept moving explains its own wait; otherwise the frames
        # the page draws in answer say what the next input will wait for.
        if target_moved(motion) is None:
            self._attach_response_frames(action, result, slow)
        return result

    def _page_script_may_block(self) -> bool:
        """An open dialog blocks page scripts until someone answers it."""

        handle = self._ensure_open()
        return handle.dialog_mode == "manual" or handle.pending_dialog is not None

    def _record_motion(self, locator: Any) -> bool:
        """Record the target's position while the input about to run waits.

        The recorder stops itself after a short window, so a fast input pays
        one tiny script call and nothing else; only a slow one reads it back.
        """

        if self._page_script_may_block():
            return False
        try:
            return bool(locator.evaluate(MOTION_RECORDER_JS, timeout=400))
        except Exception:  # noqa: BLE001 - diagnosis only; the input still runs
            return False

    def _read_motion(self) -> Optional[Mapping[str, Any]]:
        if self._page_script_may_block():
            return None
        try:
            value = self._active_page().evaluate(MOTION_READ_JS)
        except Exception:  # noqa: BLE001 - the input may have navigated away
            return None
        return value if isinstance(value, Mapping) else None

    def _input_timeout(self, action: str, exc: BaseException) -> BaseException:
        """Explain an input timeout: a target that never held still, or slow frames."""

        if unstable_target_timeout(exc):
            return BrowserError(MOVING_TIMEOUT_NOTE.format(action=action) + str(exc)[:600])
        frame_ms = int(getattr(self, "_last_frame_ms", 0) or 0)
        if frame_ms < SLOW_FRAME_MS or "Timeout" not in str(exc):
            return exc
        return BrowserError(
            INPUT_TIMEOUT_NOTE.format(action=action, ms=frame_ms) + str(exc)[:600]
        )

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
        if payload.get("selector") is None and payload.get("ref") is None:
            # The whole page is the common reading action: an agent that has
            # just taken a snapshot should not need to guess a selector. The
            # race logged one refused ``get_text`` for exactly this reason.
            try:
                text = page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
            except Exception as exc:
                raise BrowserError("page text could not be read") from exc
            raw = str(text or "")
            return {
                "text": str(redact(raw[:_MAX_TEXT_CHARS])),
                "truncated": len(raw) > _MAX_TEXT_CHARS,
                "target": "page",
            }
        locator = self._locator(payload, page)
        text = locator.inner_text(timeout=self._timeout_ms(deadline_seconds))
        clipped = text[:_MAX_TEXT_CHARS]
        return {"text": str(redact(clipped)), "truncated": len(text) > _MAX_TEXT_CHARS}

    def _evaluate(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Run the agent's script in a page served from this machine.

        ``expression`` is a JS expression, an arrow function, or statements
        that ``return`` a value; promises are awaited and the JSON value comes
        back bounded. Pages from other hosts are refused: there the agent is
        not the page's author, and a script could read what the user is
        signed in to.
        """

        self._assert_agent_input_allowed()
        page = self._active_page()
        if not self._is_local_page_url(str(getattr(page, "url", "") or "")):
            raise BrowserSecurityError(
                "evaluate runs only on pages served from this machine (open "
                "page=... or a loopback URL); use get_text or snapshot elsewhere"
            )
        raw = payload.get("expression")
        if raw is None:
            raw = payload.get("script", payload.get("code", payload.get("js")))
        if not isinstance(raw, str) or not raw.strip():
            raise BrowserError("evaluate needs expression: a JS expression or arrow function")
        if len(raw) > MAX_EXPRESSION_CHARS:
            raise BrowserError(
                f"expression must be at most {MAX_EXPRESSION_CHARS} characters"
            )
        timeout_ms = evaluate_timeout_ms(payload, deadline_seconds)
        started = time.monotonic()
        try:
            value = page.evaluate(expression_source(raw, timeout_ms))
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_syntax_error(exc):
                raise BrowserError(f"evaluate failed: {str(exc)[:800]}") from None
            try:
                value = page.evaluate(body_source(raw, timeout_ms))
            except Exception as body_exc:  # noqa: BLE001
                raise BrowserError(
                    "evaluate failed: "
                    f"{str(body_exc)[:700]} (pass one expression, an arrow "
                    "function, or statements ending in return)"
                ) from None
        result = bounded_value(redact(value))
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    def _census(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Per-object coverage of the three.js scene the page last drew (see census.py)."""

        page = self._active_page()
        if not self._is_local_page_url(str(getattr(page, "url", "") or "")):
            raise BrowserSecurityError("census runs only on pages served from this machine (open page=...)")
        started = time.monotonic()
        try:
            value = page.evaluate(census.CENSUS_JS, {"width": census.census_width(payload.get("width"))})
        except Exception as exc:  # noqa: BLE001 - reported to the model
            raise BrowserError(f"census failed: {str(exc)[:600]}") from None
        result = value if isinstance(value, dict) else {"error": "census returned no table"}
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

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
        # A child frame is attached at about:blank and navigates to its src
        # afterwards. Evaluating in that window raced the navigation: the
        # context was destroyed mid-call and five reads in six failed on a
        # page that had finished loading a moment later. Wait for the frame's
        # document, and retry a read that lost the race a bounded few times.
        try:
            validate_browser_url(str(frame.url), self.policy)
        except BrowserSecurityError:
            raise
        except Exception:  # noqa: BLE001 - checked again once the frame loaded
            pass
        timeout_ms = self._timeout_ms(deadline_seconds)
        data: Any = None
        failure: Optional[BaseException] = None
        for _attempt in range(_IFRAME_READ_ATTEMPTS):
            if cancel is not None and cancel.cancelled:
                raise BrowserCancelled("browser call cancelled")
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 - the read below reports the outcome
                pass
            try:
                data = frame.evaluate(snapshot_probe(_MAX_SNAPSHOT_NODES))
                failure = None
                break
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                failure = exc
                try:
                    page.wait_for_timeout(_IFRAME_RETRY_MS)
                except Exception:  # noqa: BLE001 - a closed page fails the read
                    break
        if failure is not None or data is None:
            raise BrowserError("iframe snapshot could not be captured") from failure
        try:
            frame_url = str(frame.url)
            validate_browser_url(frame_url, self.policy)
        except BrowserSecurityError:
            raise
        except Exception:
            frame_url = ""
        return {
            "selector": selector,
            "url": safe_network_url(frame_url),
            "snapshot": redact(dict(data or {})),
        }
