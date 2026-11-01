"""Keep automation browsers from eating the machine they run on.

A page the agent opened stays open between its browser calls, often for the
whole task, because closing the browser is a user decision. A WebGL page in a
headless Chromium renders on the CPU (SwiftShader) and its ``requestAnimationFrame``
loop never stops: a measured light three.js scene held 190% CPU on a two-core
machine between calls, doing work nobody would look at. Three parallel runs did
that to a developer's PC until every window lagged.

Two measures, both generic and both off-switchable:

* **Parking.** Once an owned headless page has been idle for a few seconds
  after a browser action it is paused through the DevTools debugger, so no
  script, timer or animation frame runs until the next action wakes it
  (190% -> ~2% CPU in the same measurement). Waking lets queued work run and
  one frame paint before the action looks at the page.
  ``Page.setWebLifecycleState("frozen")`` was tried first and measurably did not
  stop the renderer, so it is not used. The cost is the same as a background
  tab: time-driven page code sees the gap as one long frame.
* **Lower priority.** Headless browser processes started by this process run
  below normal priority (Windows ``BELOW_NORMAL``, POSIX ``nice`` 10), so the
  frames they do render never starve the user's interactive programs.

``KAROX_BROWSER_PARK=0`` and ``KAROX_BROWSER_LOW_PRIORITY=0`` restore the old
behaviour; ``KAROX_BROWSER_PARK_AFTER`` sets the idle grace in seconds. Every
helper here is fail-open: a browser that cannot be parked or re-prioritised
keeps working exactly as before.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

_OFF = {"0", "false", "no", "off"}

#: Executable names of Chromium-family browser processes (lower-cased, matched
#: as substrings so ``chrome.exe``, ``chrome-headless-shell`` and
#: ``headless_shell`` all qualify).
_BROWSER_NAMES = ("chrome", "chromium", "headless_shell", "msedge")

#: POSIX niceness given to headless browsers. Raising niceness needs no
#: privilege; it is never lowered back.
POSIX_NICENESS = 10


def _enabled(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in _OFF


def parking_enabled() -> bool:
    """Whether idle owned headless pages are paused between actions."""

    return _enabled("KAROX_BROWSER_PARK")


def low_priority_enabled() -> bool:
    """Whether headless browser processes are moved below normal priority."""

    return _enabled("KAROX_BROWSER_LOW_PRIORITY")


#: Seconds a page keeps running after an action before it is parked. Short
#: gaps (a quick ``sleep`` between two calls, back-to-back actions) keep
#: real-time semantics; the long gaps -- a model thinking for half a minute --
#: are where the CPU went.
DEFAULT_PARK_AFTER_SECONDS = 5.0


def park_after_seconds() -> float:
    """Idle grace before parking, from ``KAROX_BROWSER_PARK_AFTER`` (seconds)."""

    raw = os.environ.get("KAROX_BROWSER_PARK_AFTER", "").strip()
    if not raw:
        return DEFAULT_PARK_AFTER_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PARK_AFTER_SECONDS
    return min(max(value, 0.0), 3600.0)


#: Run after waking the active tab: let the work queued while it was parked
#: (timers, network callbacks) run and one frame paint before the action
#: looks at the page, bounded so a slow software-rendered scene cannot stall it.
SETTLE_SCRIPT = """() => new Promise((done) => {
  let finished = false;
  const finish = () => { if (!finished) { finished = true; done(true); } };
  requestAnimationFrame(() => requestAnimationFrame(finish));
  setTimeout(finish, 750);
})"""


def park_page(context: Any, page: Any) -> Optional[Any]:
    """Pause ``page``'s scripts and animation frames; return the CDP session.

    Returns ``None`` when the page cannot be parked (closed page, a browser
    without CDP, a protocol error); the caller then simply leaves it running.
    """

    session = None
    try:
        session = context.new_cdp_session(page)
        session.send("Debugger.enable")
        session.send("Debugger.pause")
        return session
    except Exception:
        if session is not None:
            wake_page(session)
        return None


def wake_page(session: Any) -> None:
    """Undo :func:`park_page`. Never raises.

    ``Debugger.resume`` fails when the pause was still pending (an idle page
    with no script to stop at); ``Debugger.disable`` then cancels it, so both
    are always sent.
    """

    for method in ("Debugger.resume", "Debugger.disable"):
        try:
            session.send(method)
        except Exception:
            pass
    try:
        session.detach()
    except Exception:
        pass


def _is_browser(proc: Any) -> bool:
    try:
        name = (proc.name() or "").lower()
    except Exception:
        return False
    return any(part in name for part in _BROWSER_NAMES)


def _is_headless_root(proc: Any) -> bool:
    try:
        name = (proc.name() or "").lower()
        if "headless" in name:
            return True
        return any(arg.startswith("--headless") for arg in proc.cmdline())
    except Exception:
        return False


def _demote(proc: Any, psutil: Any) -> bool:
    try:
        current = proc.nice()
        if sys.platform == "win32":
            raised = {
                psutil.NORMAL_PRIORITY_CLASS,
                psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                psutil.HIGH_PRIORITY_CLASS,
            }
            if current not in raised:
                return False
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return True
        if current >= POSIX_NICENESS:
            return False
        proc.nice(POSIX_NICENESS)
        return True
    except Exception:
        return False


def demote_headless_browsers(root_pid: Optional[int] = None) -> int:
    """Lower the priority of headless browser trees started by ``root_pid``.

    Only Chromium-family processes that descend from this process and are, or
    descend from, a headless browser process are touched: a visible browser the
    user may be working in keeps its priority. Returns how many processes were
    changed. Never raises.
    """

    if not low_priority_enabled():
        return 0
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return 0
    try:
        descendants = psutil.Process(root_pid or os.getpid()).children(recursive=True)
    except Exception:
        return 0
    changed = 0
    for proc in descendants:
        if not _is_browser(proc) or not _is_headless_root(proc):
            continue
        tree = [proc]
        try:
            tree.extend(proc.children(recursive=True))
        except Exception:
            pass
        for member in tree:
            if _demote(member, psutil):
                changed += 1
    return changed
