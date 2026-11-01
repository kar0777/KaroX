"""Idle parking and low priority for automation browsers (browser/resources.py).

Unit level: fakes stand in for Playwright and psutil, so these run everywhere.
The live counterpart (a real Chromium page that stops drawing while parked)
is ``test_idle_page_is_parked_between_actions`` in ``test_browser_live.py``.
"""

from __future__ import annotations

import sys
import types
import uuid

import pytest

from karox.artifacts import ArtifactStore
from karox.browser import resources
from karox.browser.netpolicy import BrowserAccessPolicy
from karox.browser.session import BrowserSession, _BrowserHandle


class FakeCdp:
    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.detached = False

    def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append(method)
        if method in self.fail:
            raise RuntimeError(f"{method} failed")
        return {}

    def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.sessions: list[FakeCdp] = []
        self.fail = fail

    def new_cdp_session(self, page: object) -> FakeCdp:
        cdp = FakeCdp(self.fail)
        self.sessions.append(cdp)
        return cdp


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.evaluated: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, script: str) -> bool:
        self.evaluated.append(script)
        return True


def test_park_pauses_through_the_debugger_and_wake_always_resumes() -> None:
    context = FakeContext()
    session = resources.park_page(context, FakePage())
    assert session is not None
    assert session.sent == ["Debugger.enable", "Debugger.pause"]
    resources.wake_page(session)
    assert session.sent[2:] == ["Debugger.resume", "Debugger.disable"]
    assert session.detached


def test_wake_cancels_a_pending_pause_when_resume_is_refused() -> None:
    # An idle page has no script to stop at: the pause stays pending and
    # Chromium refuses resume. disable must still be sent to cancel it.
    session = FakeCdp(fail=("Debugger.resume",))
    resources.wake_page(session)
    assert session.sent == ["Debugger.resume", "Debugger.disable"]


def test_park_fails_open_and_leaves_nothing_paused() -> None:
    context = FakeContext(fail=("Debugger.pause",))
    assert resources.park_page(context, FakePage()) is None
    (cdp,) = context.sessions
    assert cdp.sent[-1] == "Debugger.disable"


@pytest.mark.parametrize("value", ["0", "false", "off", "NO"])
def test_switches_turn_both_measures_off(monkeypatch, value) -> None:
    monkeypatch.setenv("KAROX_BROWSER_PARK", value)
    monkeypatch.setenv("KAROX_BROWSER_LOW_PRIORITY", value)
    assert not resources.parking_enabled()
    assert not resources.low_priority_enabled()
    assert resources.demote_headless_browsers() == 0


def test_measures_are_on_by_default(monkeypatch) -> None:
    monkeypatch.delenv("KAROX_BROWSER_PARK", raising=False)
    monkeypatch.delenv("KAROX_BROWSER_LOW_PRIORITY", raising=False)
    assert resources.parking_enabled()
    assert resources.low_priority_enabled()


class FakeProc:
    def __init__(self, name: str, cmdline: list[str], children: list["FakeProc"] = (),
                 nice: int = 0) -> None:
        self._name = name
        self._cmdline = cmdline
        self._children = list(children)
        self.priority = nice

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def children(self, recursive: bool = False) -> list["FakeProc"]:
        out = []
        for child in self._children:
            out.append(child)
            if recursive:
                out.extend(child.children(recursive=True))
        return out

    def nice(self, value: int | None = None) -> int:
        if value is not None:
            self.priority = value
        return self.priority


def _fake_psutil(root: FakeProc) -> types.ModuleType:
    module = types.ModuleType("psutil")
    module.Process = lambda pid=None: root  # type: ignore[attr-defined]
    module.NORMAL_PRIORITY_CLASS = 32  # type: ignore[attr-defined]
    module.ABOVE_NORMAL_PRIORITY_CLASS = 32768  # type: ignore[attr-defined]
    module.HIGH_PRIORITY_CLASS = 128  # type: ignore[attr-defined]
    module.BELOW_NORMAL_PRIORITY_CLASS = 16384  # type: ignore[attr-defined]
    return module


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_only_headless_browser_trees_are_demoted(monkeypatch, platform) -> None:
    normal = 32 if platform == "win32" else 0
    renderer = FakeProc("chrome-headless-shell", ["--type=renderer"], nice=normal)
    gpu = FakeProc("chrome", ["--type=gpu-process"], nice=normal)
    headless = FakeProc("chrome", ["chrome", "--headless", "--no-sandbox"],
                        children=[renderer, gpu], nice=normal)
    visible = FakeProc("chrome", ["chrome", "--user-data-dir=x"], nice=normal)
    driver = FakeProc("node", ["node", "cli.js", "run-driver"], children=[headless, visible],
                      nice=normal)
    me = FakeProc("python", ["python"], children=[driver], nice=normal)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(me))
    monkeypatch.setattr(resources.sys, "platform", platform)
    monkeypatch.delenv("KAROX_BROWSER_LOW_PRIORITY", raising=False)

    changed = resources.demote_headless_browsers()

    lowered = 16384 if platform == "win32" else resources.POSIX_NICENESS
    assert changed == 3
    assert headless.priority == renderer.priority == gpu.priority == lowered
    assert visible.priority == normal
    assert driver.priority == me.priority == normal
    # Idempotent: a second pass finds nothing left to change.
    assert resources.demote_headless_browsers() == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux niceness is per thread")
def test_linux_demotion_moves_every_thread_of_a_real_process() -> None:
    import os
    import subprocess

    psutil = pytest.importorskip("psutil")
    if os.getpriority(os.PRIO_PROCESS, 0) >= resources.POSIX_NICENESS:
        pytest.skip("this test process already runs at the browser niceness")
    code = (
        "import threading, time\n"
        "for _ in range(3):\n"
        "    threading.Thread(target=time.sleep, args=(60,), daemon=True).start()\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        proc = psutil.Process(child.pid)
        tids = [thread.id for thread in proc.threads()]
        assert len(tids) >= 4
        assert any(os.getpriority(os.PRIO_PROCESS, tid) < resources.POSIX_NICENESS for tid in tids)

        assert resources._demote(proc, psutil) is True

        # Not just the main thread: every worker moved.
        assert all(
            os.getpriority(os.PRIO_PROCESS, tid) >= resources.POSIX_NICENESS for tid in tids
        )
        assert resources._demote(proc, psutil) is False
    finally:
        child.kill()
        child.wait()


def test_demotion_never_raises(monkeypatch) -> None:
    module = types.ModuleType("psutil")

    def boom(pid=None):
        raise RuntimeError("no such process")

    module.Process = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", module)
    monkeypatch.delenv("KAROX_BROWSER_LOW_PRIORITY", raising=False)
    assert resources.demote_headless_browsers() == 0


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.delenv("KAROX_BROWSER_PARK", raising=False)
    monkeypatch.setenv("KAROX_BROWSER_PARK_AFTER", "0")
    monkeypatch.setattr("karox.browser.session.demote_headless_browsers", lambda: 0)
    created: list[BrowserSession] = []

    def make(*, headed: bool = False) -> tuple[BrowserSession, FakeContext, dict]:
        session_id = f"park-{uuid.uuid4().hex[:8]}"
        artifacts = ArtifactStore(session_id, root=tmp_path / session_id)
        policy = BrowserAccessPolicy(session_id=session_id, localhost=True, headed=headed)
        browser = BrowserSession(artifacts, policy, browser_root=tmp_path / "browser")
        context = FakeContext()
        tabs = {"tab-1": FakePage(), "tab-2": FakePage()}
        browser._handle = _BrowserHandle(
            playwright_ctx=None, browser=None, context=context, tabs=tabs,
            active_tab_id="tab-1",
        )
        created.append(browser)
        return browser, context, tabs

    yield make
    for browser in created:
        browser._cancel_park_timer()
        browser._handle = None


def _run(browser: BrowserSession, seen: list) -> None:
    def action(self, payload, deadline_seconds, cancel):
        seen.append(dict(self._handle.parked))
        return {"ok": True}

    browser._call_on_owner(type(browser)._run_unparked, action, {}, 5.0, None)


def test_tabs_are_awake_during_an_action_and_parked_after_it(session) -> None:
    browser, context, tabs = session()
    seen: list = []
    _run(browser, seen)
    assert seen == [{}]
    assert set(browser._handle.parked) == {"tab-1", "tab-2"}
    first = dict(browser._handle.parked)
    assert all(cdp.sent == ["Debugger.enable", "Debugger.pause"] for cdp in first.values())

    _run(browser, seen)
    assert seen[1] == {}, "the next action must see every tab awake"
    assert all(cdp.sent[2:] == ["Debugger.resume", "Debugger.disable"] for cdp in first.values())
    assert set(browser._handle.parked) == {"tab-1", "tab-2"}


def test_a_failing_action_still_parks(session) -> None:
    browser, _context, _tabs = session()

    def broken(self, payload, deadline_seconds, cancel):
        raise RuntimeError("action failed")

    with pytest.raises(RuntimeError):
        browser._call_on_owner(type(browser)._run_unparked, broken, {}, 5.0, None)
    assert set(browser._handle.parked) == {"tab-1", "tab-2"}


def test_closed_tabs_are_not_parked(session) -> None:
    browser, _context, tabs = session()
    tabs["tab-2"].closed = True
    _run(browser, [])
    assert set(browser._handle.parked) == {"tab-1"}


@pytest.mark.parametrize("case", ["headed", "takeover", "detached", "cdp", "switch_off"])
def test_browsers_someone_may_watch_are_never_parked(session, monkeypatch, case) -> None:
    browser, context, _tabs = session(headed=case == "headed")
    if case == "takeover":
        browser._handle.takeover_active = True
    elif case == "detached":
        browser._handle.detached = True
    elif case == "cdp":
        browser._handle.cdp_browser = True
    elif case == "switch_off":
        monkeypatch.setenv("KAROX_BROWSER_PARK", "0")
    _run(browser, [])
    assert browser._handle.parked == {}
    assert context.sessions == []


def test_force_close_wakes_parked_tabs_first(session) -> None:
    browser, _context, _tabs = session()
    _run(browser, [])
    parked = list(browser._handle.parked.values())
    result = browser.force_close()
    assert result["closed"] is True and browser._handle is None
    assert all(cdp.sent[-2:] == ["Debugger.resume", "Debugger.disable"] for cdp in parked)


def test_waking_lets_the_active_tab_settle_before_the_action(session) -> None:
    browser, _context, tabs = session()
    _run(browser, [])
    _run(browser, [])
    assert tabs["tab-1"].evaluated == [resources.SETTLE_SCRIPT]
    assert tabs["tab-2"].evaluated == [], "background tabs are not settled"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 5.0), ("12", 12.0), ("0", 0.0), ("-3", 0.0), ("junk", 5.0), ("99999", 3600.0)],
)
def test_park_after_reads_a_bounded_grace(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("KAROX_BROWSER_PARK_AFTER", raw)
    assert resources.park_after_seconds() == expected


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_pages_park_only_after_the_idle_grace(session, monkeypatch) -> None:
    import time

    browser, _context, _tabs = session()
    monkeypatch.setenv("KAROX_BROWSER_PARK_AFTER", "0.3")
    _run(browser, [])
    assert browser._handle.parked == {}, "a page must keep running right after an action"
    assert _wait_until(lambda: set(browser._handle.parked) == {"tab-1", "tab-2"})
    time.sleep(0.05)


def test_a_new_action_cancels_a_pending_park(session, monkeypatch) -> None:
    import time

    browser, context, _tabs = session()
    monkeypatch.setenv("KAROX_BROWSER_PARK_AFTER", "0.4")
    _run(browser, [])
    time.sleep(0.2)
    _run(browser, [])  # restarts the grace
    time.sleep(0.3)
    assert browser._handle.parked == {}, "the first action's park must be cancelled"
    assert context.sessions == []
    assert _wait_until(lambda: set(browser._handle.parked) == {"tab-1", "tab-2"})


def test_a_stale_park_does_nothing(session) -> None:
    browser, context, _tabs = session()
    browser._activity = 7
    browser._call_on_owner(type(browser)._park_if_idle, 6)
    assert browser._handle.parked == {} and context.sessions == []
    browser._call_on_owner(type(browser)._park_if_idle, 7)
    assert set(browser._handle.parked) == {"tab-1", "tab-2"}

