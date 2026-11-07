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
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from ..browser_bootstrap import BrowserBootstrapError, ensure_playwright_chromium
from ..paths import runtime_dir
from ..process_identity import (
    capture_process_identity,
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
    validate_browser_url,
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
from .contract import (
    DEFAULT_VIEWPORT_WIDTH,
    DEFAULT_VIEWPORT_HEIGHT,
    _MAX_SNAPSHOT_NODES,
    _INSTANCE_SCHEMA_VERSION,
    CancelToken,
    _BrowserHandle,
    _BrowserCommand,
    ACTION_CAPABILITIES,
    INPUT_ACTIONS,
    READ_ACTIONS,
)


_PLAYWRIGHT_MISSING = (
    "browser automation requires the optional Playwright package and browser; "
    "install the 'browser' extra and run `python -m playwright install chromium`"
)


#: Modifiers that turn a letter into a shortcut instead of inserted text.














def _windowless_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class BrowserSession(BrowserEvidenceMixin, BrowserWaitsMixin, BrowserInteractionMixin, BrowserObservationMixin, BrowserCollectorsMixin, BrowserNavigationMixin, BrowserRuntimeMixin, SessionGuardsMixin):
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

    def _open(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        url = validate_browser_url(payload.get("url"), self.policy)
        # The default matches the visual checker's frame (1280x800): what the
        # run inspects is what the checker judges, and a software-rendered
        # scene draws ~20% fewer pixels per frame than at 1440x900.
        width = int(payload.get("width", DEFAULT_VIEWPORT_WIDTH))
        height = int(payload.get("height", DEFAULT_VIEWPORT_HEIGHT))
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            raise BrowserError(
                "browser viewport is outside safe bounds (320-3840 x 240-2160)"
            )
        if self._handle is not None:
            self._assert_agent_input_allowed()
            page = self._active_page()
            if "width" in payload or "height" in payload:
                # A reused tab used to keep its first viewport silently: a run
                # asked for 390x844, read a 1440x900 layout as the phone view,
                # then relaunched a detached browser to get the size it named.
                self._resize(page, width, height)
            self._navigate(page, url, deadline_seconds)
            handle = self._ensure_open()
            reused: dict[str, Any] = {
                "open": True,
                "reused": True,
                "url": self._safe_url(page),
                "tab_id": handle.active_tab_id,
                "session_id": self.policy.session_id,
                "context_id": handle.context_id,
                "context_preserved": True,
                "engine": handle.engine,
                "profile": handle.profile,
                "detached": handle.detached,
            }
            viewport = getattr(page, "viewport_size", None)
            if isinstance(viewport, dict) and viewport:
                reused["viewport"] = dict(viewport)
            self._attach_frame_report(page, reused)
            return reused
        profile = payload.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise BrowserError("profile must be a safe identifier string")
        detach = bool(payload.get("detach", False))
        if detach and not self.policy.allow_detach:
            raise BrowserSecurityError(
                "detached browser instances are not enabled for this session"
            )
        try:
            executable = ensure_playwright_chromium()
        except BrowserBootstrapError as exc:
            raise BrowserError(str(exc)) from None
        if detach:
            return self._open_detached(
                url,
                executable,
                profile=str(profile) if profile else f"detached-{uuid.uuid4().hex[:8]}",
                width=width,
                height=height,
                deadline_seconds=deadline_seconds,
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(_PLAYWRIGHT_MISSING) from exc
        ctx = sync_playwright().start()
        browser = None
        context = None
        proxy: Optional[SessionPinnedProxy] = None
        profile_dir: Optional[Path] = None
        try:
            proxy = SessionPinnedProxy(self.policy)
            proxy.start()
            proxy_settings = {
                "server": proxy.server_url,
                "username": proxy.username,
                "password": proxy.password,
            }
            if self.policy.headed:
                browser = ctx.chromium.launch(headless=False, proxy=proxy_settings)
            else:
                # Headless WebGL is software-rendered unless asked otherwise;
                # see browser/render_backend.py for the measured cost.
                from .render_backend import launch_args, software_args

                try:
                    browser = ctx.chromium.launch(
                        headless=True, proxy=proxy_settings, args=launch_args()
                    )
                except Exception:
                    browser = ctx.chromium.launch(
                        headless=True, proxy=proxy_settings, args=software_args()
                    )
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": width, "height": height},
                "accept_downloads": bool(self.policy.downloads),
                "service_workers": "block",
            }
            if profile:
                profile_dir = self._profile_dir(str(profile))
                profile_dir.mkdir(parents=True, exist_ok=True)
                state_path = profile_dir / "storage_state.json"
                if state_path.is_file():
                    context_kwargs["storage_state"] = str(state_path)
            context = browser.new_context(**context_kwargs)
            context.route("**/*", self._route)
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=proxy,
                profile=str(profile) if profile else None,
                profile_dir=profile_dir,
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            page = context.new_page()
            tab_id = self._register_page(page, make_active=True)
            self._navigate(page, url, deadline_seconds)
        except Exception:
            self._handle = None
            self._teardown_quietly(context, browser, proxy, ctx, None)
            raise
        opened: dict[str, Any] = {
            "open": True,
            "url": self._safe_url(page),
            "viewport": {"width": width, "height": height},
            "session_id": self.policy.session_id,
            "tab_id": tab_id,
            "context_id": handle.context_id,
            "headed": self.policy.headed,
            "context_isolated": True,
            "context_preserved": True,
            "engine": handle.engine,
            "profile": handle.profile,
            "detached": False,
        }
        self._attach_frame_report(page, opened)
        return opened











    def _open_detached(
        self,
        url: str,
        executable: Path,
        profile: str,
        *,
        width: int,
        height: int,
        deadline_seconds: float,
    ) -> dict[str, Any]:
        """Launch an ownership-proven detached Chromium and attach over CDP.

        The instance survives this KaroX process: the PID, its OS creation
        time, the loopback CDP port and the profile directory are persisted,
        and ``reconnect`` later proves the process identity before attaching.

        Detached instances are loopback-only. A bare Chromium command line
        cannot answer the pinning proxy's per-session authentication (v5 did
        that through its Chrome extension, which this line does not ship), so
        external HTTPS research stays on the managed session, which keeps the
        proxy and still persists cookies through named profiles.
        """

        if self.policy.external_https:
            raise BrowserSecurityError(
                "detached browser instances are loopback-only; use a managed "
                "session for external HTTPS research"
            )
        from playwright.sync_api import sync_playwright

        profile_dir = self._profile_dir(profile)
        userdata = profile_dir / "userdata"
        userdata.mkdir(parents=True, exist_ok=True)
        marker = userdata / "DevToolsActivePort"
        try:
            marker.unlink()
        except OSError:
            pass
        argv = [
            str(executable),
            f"--user-data-dir={userdata}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=TranslateUI",
            f"--window-size={width},{height}",
            "about:blank",
        ]
        if not self.policy.headed:
            argv.insert(1, "--headless=new")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, KaroX-owned binary
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_windowless_flags(),
            close_fds=True,
        )
        ctx = None
        port = 0
        ws_url = ""
        try:
            deadline = time.monotonic() + max(10.0, min(deadline_seconds, 60.0))
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BrowserError("detached browser exited during startup")
                if marker.is_file():
                    try:
                        lines = marker.read_text(encoding="ascii").splitlines()
                    except OSError:
                        lines = []
                    if len(lines) >= 2 and lines[0].strip().isdigit():
                        port = int(lines[0].strip())
                        ws_url = f"ws://127.0.0.1:{port}{lines[1].strip()}"
                        break
                time.sleep(0.1)
            if not ws_url:
                raise BrowserError("detached browser did not open a debugging port")
            ctx = sync_playwright().start()
            browser = ctx.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.set_default_timeout(30_000)
            instance_id = f"inst-{uuid.uuid4().hex[:16]}"
            identity = capture_process_identity(process.pid, owner=self.policy.session_id)
            record = {
                "schema": _INSTANCE_SCHEMA_VERSION,
                "instance_id": instance_id,
                "session_id": self.policy.session_id,
                "pid": process.pid,
                "create_time_ns": identity.create_time_ns,
                "port": port,
                "ws_url": ws_url,
                "profile": profile,
                "started_at": time.time(),
                "headed": self.policy.headed,
            }
            instances = self._instances_dir()
            instances.mkdir(parents=True, exist_ok=True)
            record_path = instances / f"{instance_id}.json"
            record_path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            try:
                os.chmod(record_path, 0o600)
            except OSError:
                pass
            context.route("**/*", self._route)
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=None,
                profile=profile,
                profile_dir=profile_dir,
                detached=True,
                instance_id=instance_id,
                browser_process=process,
                cdp_browser=True,
                engine="playwright_cdp_detached",
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            for existing_page in list(context.pages):
                self._register_page(existing_page, make_active=False)
            page = context.new_page()
            tab_id = self._register_page(page, make_active=True)
            self._navigate(page, url, deadline_seconds)
        except Exception:
            self._handle = None
            self._terminate_process(process)
            try:
                if ctx is not None:
                    ctx.stop()
            except Exception:
                pass
            raise
        return {
            "open": True,
            "detached": True,
            "instance_id": instance_id,
            "url": self._safe_url(page),
            "viewport": {"width": width, "height": height},
            "session_id": self.policy.session_id,
            "tab_id": tab_id,
            "context_id": handle.context_id,
            "headed": self.policy.headed,
            "engine": handle.engine,
            "profile": profile,
            "profile_persistent": True,
        }

    def _reconnect(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Attach to a detached instance this KaroX line launched, or fail closed."""

        if self._handle is not None:
            raise BrowserSessionError(
                "browser session is already open; close it before reconnecting"
            )
        if not self.policy.allow_detach:
            raise BrowserSecurityError(
                "detached browser instances are not enabled for this session"
            )
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not re.fullmatch(
            r"inst-[0-9a-f]{16}", instance_id
        ):
            raise BrowserError("instance_id must be a value returned by a detached open")
        record_path = self._instances_dir() / f"{instance_id}.json"
        if not record_path.is_file():
            raise BrowserSessionError(f"browser instance is not registered: {instance_id}")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrowserSessionError("browser instance record is unreadable") from exc
        pid = int(record.get("pid") or 0)
        create_time_ns = record.get("create_time_ns")
        ws_url = str(record.get("ws_url") or "")
        if not pid or not ws_url.startswith("ws://127.0.0.1:"):
            raise BrowserSessionError("browser instance record is malformed")
        if create_time_ns is None:
            # Fail closed: without a recorded creation time a recycled PID is
            # indistinguishable from the browser KaroX launched.
            raise BrowserSessionError(
                "browser instance identity was never proven; refusing to attach"
            )
        if not process_is_running(pid):
            raise BrowserSessionError(
                f"browser instance {instance_id} is no longer running"
            )
        live_create_time = read_process_create_time_ns(pid)
        if live_create_time is None or int(live_create_time) != int(create_time_ns):
            raise BrowserSessionError(
                "browser instance PID was reused by another process; refusing to attach"
            )
        from playwright.sync_api import sync_playwright

        ctx = sync_playwright().start()
        try:
            browser = ctx.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.route("**/*", self._route)
            profile = record.get("profile")
            profile_dir = self._profile_dir(str(profile)) if profile else None
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=None,
                profile=str(profile) if profile else None,
                profile_dir=profile_dir,
                detached=True,
                instance_id=instance_id,
                cdp_browser=True,
                engine="playwright_cdp_detached",
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))
            tab_ids = [
                self._register_page(existing, make_active=False)
                for existing in list(context.pages)
            ]
            if not handle.tabs:
                page = context.new_page()
                tab_ids = [self._register_page(page, make_active=True)]
            else:
                handle.active_tab_id = tab_ids[0]
        except Exception:
            self._handle = None
            try:
                ctx.stop()
            except Exception:
                pass
            raise
        return {
            "reconnected": True,
            "instance_id": instance_id,
            "pid": pid,
            "tab_ids": tab_ids,
            "active_tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "engine": handle.engine,
            "url": self._safe_url(self._active_page()),
        }

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