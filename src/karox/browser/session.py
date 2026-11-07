"""One stateful Playwright browser session owned by one KaroX session.

This is the engine the ``browser.command`` tool surface drives. It ports the
proven KaroX v5 ``browser_access`` session manager (owner-thread marshalling,
per-request URL validation, tab ownership, takeover, payment guard, bounded
redacted collectors) and completes the action surface the release risk
vocabulary already names: scrolling, hovering, typed keys, form state, text
search, snapshots with diffs, dialogs, iframes, uploads, downloads, cookie
persistence through named profiles, detached instances with ownership-proven
CDP reconnect, and stale-element recovery.

Security posture:

* Every URL is validated against :class:`BrowserAccessPolicy` before
  navigation *and* on every routed request, and again after navigation, so
  redirects and DNS rebinding are refused at the boundary they cross. A
  per-session authenticated loopback proxy performs the final upstream DNS
  lookup and pins the validated address.
* No tool result carries a cookie value, a password value, a storage blob, or
  an unbounded body. Text is size-capped and passed through the redaction
  filter; screenshots and downloads land in the session's artifact store and
  return references.
* Password-shaped fields refuse plain fills unless the policy explicitly
  enables ``plain_credentials`` (user-supplied test accounts); the value is
  then masked in the page before the fill and never echoed back. Keyring
  credential injection is always available through ``fill_credential``.
* CAPTCHA/2FA/payment/consent belong to the human: ``request_user_takeover``
  pauses every agent input until ``resume_after_user_takeover`` proves the
  page is back on an allowed URL.

Playwright's synchronous API is greenlet-based and every object it creates is
bound to the thread that started ``sync_playwright()``. Callers may arrive on
any thread, so every operation is marshalled to one permanent, session-owned
worker thread.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from ..paths import runtime_dir
from ..process_identity import (
    process_is_running,
    read_process_create_time_ns,
)
from .credentials import (
    BrowserCredentialStore,
)
from .errors import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionError,
)
from .runtime import BrowserRuntimeMixin
from .netpolicy import (
    BrowserAccessPolicy,
    SessionPinnedProxy,
    is_local_browser_url,
)
from .guards import (
    SessionGuardsMixin,
)
from .probes import (
    snapshot_probe,
)
from .resources import demote_headless_browsers
from .navigation import BrowserNavigationMixin
from .collectors import BrowserCollectorsMixin
from .observation import BrowserObservationMixin
from .interaction import BrowserInteractionMixin
from .waits import BrowserWaitsMixin
from .evidence import BrowserEvidenceMixin
from .launch import BrowserLaunchMixin
from .contract import (
    _windowless_flags,
    _MAX_SNAPSHOT_NODES,
    CancelToken,
    _BrowserHandle,
    _BrowserCommand,
    ACTION_CAPABILITIES,
    INPUT_ACTIONS,
    READ_ACTIONS,
)




#: Modifiers that turn a letter into a shortcut instead of inserted text.
















class BrowserSession(BrowserLaunchMixin, BrowserEvidenceMixin, BrowserWaitsMixin, BrowserInteractionMixin, BrowserObservationMixin, BrowserCollectorsMixin, BrowserNavigationMixin, BrowserRuntimeMixin, SessionGuardsMixin):
    """Own one Playwright browser/context for exactly one KaroX session."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        policy: BrowserAccessPolicy,
        credential_store: Optional[BrowserCredentialStore] = None,
        *,
        upload_roots: Sequence[Path] = (),
        browser_root: Optional[Path] = None,
    ) -> None:
        if artifacts.session_id != policy.session_id:
            raise ValueError("browser policy and artifact store session IDs differ")
        self._artifacts = artifacts
        self.policy = policy
        self._credential_store = credential_store or BrowserCredentialStore()
        self._upload_roots = tuple(Path(root) for root in upload_roots)
        self._browser_root = (
            Path(browser_root)
            if browser_root is not None
            else runtime_dir() / "vnext" / "browser"
        )
        self._handle: Optional[_BrowserHandle] = None
        self._explicit_navigation_target: Optional[str] = None
        # Idle parking: every action bumps the generation; a park scheduled
        # for an older generation is stale and does nothing.
        self._activity = 0
        self._park_timer: Optional[threading.Timer] = None

        self._owner_queue: "queue.Queue[Optional[_BrowserCommand]]" = queue.Queue()
        self._owner_ready = threading.Event()
        self._owner_thread_id: Optional[int] = None
        self._owner_thread = threading.Thread(
            target=self._owner_loop,
            name=f"karox-browser-owner-{policy.session_id[:24]}",
            daemon=True,
        )
        self._owner_thread.start()
        if not self._owner_ready.wait(timeout=5.0):
            raise BrowserSessionError("browser owner thread did not start")

    def _is_local_page_url(self, url: str) -> bool:
        """Compatibility seam: tests patch ``karox.browser.session``."""

        return is_local_browser_url(url)

    def _artifact_byte_limit(self) -> int:
        """Compatibility seam: tests patch ``karox.browser.session``."""

        return MAX_ARTIFACT_BYTES

    def _demote_browser_processes(self) -> int:
        """Compatibility seam: tests patch ``karox.browser.session``."""

        return demote_headless_browsers()

    # -- owner-thread marshalling ------------------------------------------



    # -- session state ------------------------------------------------------












    # -- dispatch ------------------------------------------------------------


    # -- idle parking (see browser/resources.py) -------------------------------










    # -- URL / page plumbing ---------------------------------------------------









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


    # -- collectors -------------------------------------------------------------



    # -- element resolution -----------------------------------------------------


    # -- lifecycle actions -------------------------------------------------------














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

    # -- tab actions ---------------------------------------------------------------





    # -- navigation ------------------------------------------------------------------





    # -- extraction ------------------------------------------------------------------

    _SNAPSHOT_PROBE = snapshot_probe(_MAX_SNAPSHOT_NODES)











    # -- element input ----------------------------------------------------------------

















    # -- waiting ----------------------------------------------------------------------





    # -- takeover ----------------------------------------------------------------------





    # -- evidence ----------------------------------------------------------------------









    # -- file transfer -------------------------------------------------------------------





    # Wrap every action exactly once, after the class body: keeps the methods
    # readable while guaranteeing Playwright never crosses threads, about:blank
    # is healed before page work, and cancellation brackets every call.


for _action_name in sorted(ACTION_CAPABILITIES):
    _method = getattr(BrowserSession, "_" + _action_name, None)
    if _method is None:
        continue
    setattr(BrowserSession, "_" + _action_name, BrowserSession._guarded(_method))


__all__ = [
    "ACTION_CAPABILITIES",
    "BrowserSession",
    "CancelToken",
    "INPUT_ACTIONS",
    "READ_ACTIONS",
]