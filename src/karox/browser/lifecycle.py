"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Mapping, Optional
from ..process_identity import (
    process_is_running,
    read_process_create_time_ns,
)
from .errors import (
    BrowserError,
    BrowserSecurityError,
)
from .netpolicy import (
    SessionPinnedProxy,
)
from .contract import (
    _windowless_flags,
    CancelToken,
    _BrowserHandle,
)


class BrowserLifecycleMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

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

        if not self._owner_thread.is_alive():
            self._handle = None
            return {
                "closed": True,
                "was_open": False,
                "already_closed": True,
                "session_id": self.policy.session_id,
            }
        result = dict(
            self._call_on_owner(
                type(self)._force_close_impl,
                timeout=30.0,
            )
        )
        # A force close is a session teardown, not the model-facing `close`
        # action.  Stop the marshalling thread so a test suite or a long-lived
        # process does not leak one owner thread per browser session.
        self._stop_owner()
        return result

    def _force_close_impl(self) -> dict[str, Any]:
        handle = self._handle
        if handle is not None:
            handle.takeover_active = False
        self._cancel_park_timer()
        self._wake_pages()
        return self._close({"user_confirmed": True, "reason": "runtime teardown"}, 30.0, None)
