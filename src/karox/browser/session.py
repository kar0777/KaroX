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

import queue
import threading
from pathlib import Path
from typing import Optional, Sequence

from ..artifacts import MAX_ARTIFACT_BYTES, ArtifactStore
from ..paths import runtime_dir
from .credentials import (
    BrowserCredentialStore,
)
from .errors import (
    BrowserSessionError,
)
from .runtime import BrowserRuntimeMixin
from .netpolicy import (
    BrowserAccessPolicy,
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
from .lifecycle import BrowserLifecycleMixin
from .contract import (
    _MAX_SNAPSHOT_NODES,
    CancelToken,
    _BrowserHandle,
    _BrowserCommand,
    ACTION_CAPABILITIES,
    INPUT_ACTIONS,
    READ_ACTIONS,
)


class BrowserSession(
    BrowserLifecycleMixin,
    BrowserLaunchMixin,
    BrowserEvidenceMixin,
    BrowserWaitsMixin,
    BrowserInteractionMixin,
    BrowserObservationMixin,
    BrowserCollectorsMixin,
    BrowserNavigationMixin,
    BrowserRuntimeMixin,
    SessionGuardsMixin,
):
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


    _SNAPSHOT_PROBE = snapshot_probe(_MAX_SNAPSHOT_NODES)


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
