"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import concurrent.futures
import functools
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional
from .errors import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
)
from .contract import (
    _MAX_SELECTOR_LEN,
    _MAX_VALUE_LEN,
    _PROFILE_ID,
    CancelToken,
    _BrowserHandle,
    _BrowserCommand,
    _AUTO_RECOVER_ACTIONS,
    ACTION_CAPABILITIES,
)
from .resources import (
    SETTLE_SCRIPT,
    park_after_seconds,
    park_page,
    parking_enabled,
    wake_page,
)

if TYPE_CHECKING:  # pragma: no cover
    from .session import BrowserSession


class BrowserRuntimeMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

    def _owner_loop(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._owner_ready.set()
        while True:
            command = self._owner_queue.get()
            if command is None:
                self._owner_queue.task_done()
                return
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

    def _call_on_owner(
        self,
        callback: Callable[..., Any],
        *args: Any,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
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
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # A callback may itself raise TimeoutError; only a still-pending
            # future is the wedged-owner case we convert.  A browser action must
            # never pin the whole host forever if the owner queue is lost.
            if future.done():
                raise
            future.cancel()
            raise BrowserSessionError(
                f"browser owner thread did not answer within {timeout:.1f}s"
            ) from None

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
        return dict(
            self._call_on_owner(
                type(self)._run_unparked,
                method,
                payload,
                deadline_seconds,
                cancel,
                timeout=float(deadline_seconds) + 5.0,
            )
        )

    def _run_unparked(
        self,
        method: Callable[..., Any],
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> Any:
        """Wake parked tabs, run one action, schedule parking once idle."""

        self._activity += 1
        self._cancel_park_timer()
        self._wake_pages()
        try:
            return method(self, payload, deadline_seconds, cancel)
        finally:
            self._schedule_park()

    def _cancel_park_timer(self) -> None:
        timer, self._park_timer = self._park_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_park(self) -> None:
        handle = self._handle
        if handle is None or not self._parking_allowed(handle):
            return
        delay = park_after_seconds()
        if delay <= 0:
            self._park_pages()
            return
        timer = threading.Timer(delay, self._park_when_idle, args=(self._activity,))
        timer.daemon = True
        self._park_timer = timer
        timer.start()

    def _park_when_idle(self, generation: int) -> None:
        # Timer thread: Playwright objects belong to the owner thread.
        try:
            self._call_on_owner(
                type(self)._park_if_idle,
                generation,
                timeout=10.0,
            )
        except Exception:
            pass

    def _park_if_idle(self, generation: int) -> None:
        if generation == self._activity:
            self._park_pages()

    def _parking_allowed(self, handle: _BrowserHandle) -> bool:
        # Only a headless browser this session launched itself: a visible or
        # handed-over browser is the user's to look at, and a detached or
        # CDP-attached instance may be watched by something else.
        return (
            parking_enabled()
            and not self.policy.headed
            and not handle.takeover_active
            and not handle.detached
            and not handle.cdp_browser
        )

    def _park_pages(self) -> None:
        """Pause every open tab until the next action. Never raises."""

        handle = self._handle
        if handle is None or not self._parking_allowed(handle):
            return
        for tab_id, page in list(handle.tabs.items()):
            if tab_id in handle.parked or self._page_closed(page):
                continue
            session = park_page(handle.context, page)
            if session is not None:
                handle.parked[tab_id] = session
        self._demote_browser_processes()

    def _wake_pages(self) -> None:
        """Resume every parked tab. Never raises."""

        handle = self._handle
        if handle is None or not handle.parked:
            return
        parked, handle.parked = handle.parked, {}
        for session in parked.values():
            wake_page(session)
        active = handle.tabs.get(handle.active_tab_id)
        if handle.active_tab_id in parked and active is not None and not self._page_closed(active):
            try:
                active.evaluate(SETTLE_SCRIPT)
            except Exception:
                pass

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

    def _stop_owner(self) -> None:
        if threading.get_ident() == self._owner_thread_id:
            return
        if not self._owner_thread.is_alive():
            return
        self._owner_queue.put(None)
        self._owner_thread.join(timeout=5.0)

    @staticmethod
    def _page_closed(page: Any) -> bool:
        try:
            return bool(page.is_closed())
        except Exception:
            return True
