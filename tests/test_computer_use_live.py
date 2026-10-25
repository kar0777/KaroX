"""Live desktop evidence against a controlled, self-owned test window.

These tests touch a REAL Windows desktop, so they are gated behind
``KAROX_DESKTOP_LIVE=1`` and never run in the deterministic suite. The target
is always the harness window from ``_desktop_test_window.py`` — created
minimized and without activation, owned by this process — never the user's
foreground session. No step injects global input, moves the pointer or
activates a foreign window; the visible-terminal proof additionally requires
``KAROX_DESKTOP_LIVE_TERMINAL=1`` because it puts a real window on screen and
is terminated by the test immediately after verification.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="live desktop evidence requires Windows"),
    pytest.mark.skipif(
        os.environ.get("KAROX_DESKTOP_LIVE") != "1",
        reason="set KAROX_DESKTOP_LIVE=1 to run real-desktop evidence",
    ),
]

from _desktop_test_window import TestWindow  # noqa: E402
from test_computer_use import _decode_png_chunks  # noqa: E402

from karox.computer_use.adapter import ComputerUseAdapter  # noqa: E402
from karox.computer_use.approval import action_digest  # noqa: E402
from karox.computer_use.win32 import default_probe  # noqa: E402


def _make_adapter(tmp_path: Path, session: str = "live") -> ComputerUseAdapter:
    return ComputerUseAdapter(runtime_root=tmp_path / "runtime", session_id=session, probe=default_probe())


def _wait_text(probe, hwnd: int, expected: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = probe.get_text(hwnd, 512)
        if text == expected:
            return text
        probe.sleep_ms(50)
    return text


def test_live_background_flow_on_controlled_window(tmp_path: Path) -> None:
    probe = default_probe()
    dpi_mode = probe.ensure_dpi_awareness()
    assert dpi_mode in {"per-monitor-v2", "per-monitor", "system"}, dpi_mode

    foreground_before = probe.foreground_hwnd()
    token = f"{os.getpid()}-{int(time.time())}"
    window = TestWindow(f"KaroX Desktop Live {token}")
    window.start()
    try:
        adapter = _make_adapter(tmp_path)
        title = f"KaroX Desktop Live {token}"

        # discovery finds the real window without touching it
        listed = adapter.execute("desktop.list_windows", {"title_contains": title})
        assert listed.ok, listed.error
        assert listed.data["count"] == 1
        row = listed.data["windows"][0]
        assert row["window_id"] == window.hwnd
        assert row["minimized"] is True
        assert row["is_foreground"] is False
        assert row["dpi"] >= 96

        # attach pins the real process identity (this python process)
        attached = adapter.execute(
            "desktop.attach",
            {"app_id": "livetest", "window_id": window.hwnd, "access": "control"},
        )
        assert attached.ok, attached.error
        identity = attached.data["window"]
        assert identity["process_id"] == os.getpid()

        import psutil

        create_time, exe = psutil.Process(os.getpid()).create_time(), psutil.Process(os.getpid()).exe()
        binding = adapter.session.bound("livetest")
        assert binding.identity.process_create_time == pytest.approx(create_time, abs=0.01)
        assert exe.lower().endswith(".exe")

        # UI inspection finds the real EDIT control
        inspected = adapter.execute("desktop.inspect_ui", {"app_id": "livetest"})
        assert inspected.ok, inspected.error
        classes = {node["class"] for node in inspected.data["nodes"]}
        assert "Edit" in classes
        edit_node = next(n for n in inspected.data["nodes"] if n["class"] == "Edit")
        assert edit_node["window_id"] == window.edit_hwnd
        assert edit_node["same_process"] is True
        assert inspected.evidence["node_count"] >= 1

        # visible restore WITHOUT activation; a real app holds thread-internal
        # focus once restored, and the harness re-establishes it on its own
        # thread — the user's foreground is never touched
        shown = adapter.execute(
            "desktop.show_window", {"app_id": "livetest", "mode": "restore_noactivate"}
        )
        assert shown.ok, shown.error
        assert shown.data["restored"] is True
        assert shown.data["activated"] is False
        assert probe.is_iconic(window.hwnd) is False
        assert probe.foreground_hwnd() != window.hwnd
        window.focus_edit()

        # background typing really lands in the EDIT control
        typed_text = "KaroX live background typing 123"
        typed = adapter.execute(
            "desktop.type_text", {"app_id": "livetest", "text": typed_text}
        )
        assert typed.ok, typed.error
        assert typed.data["focus_source"] == "thread_focus"
        assert typed.evidence["redacted"]["text_chars"] == len(typed_text)
        assert typed_text not in json.dumps(typed.evidence)
        assert _wait_text(probe, window.edit_hwnd, typed_text) == typed_text

        # navigation keys really move the caret in the live control
        home = adapter.execute("desktop.press_key", {"app_id": "livetest", "key": "home"})
        assert home.ok, home.error
        assert home.data["focus_source"] == "thread_focus"
        prefix = adapter.execute("desktop.type_text", {"app_id": "livetest", "text": "Y"})
        assert prefix.ok, prefix.error
        expected = "Y" + typed_text
        assert _wait_text(probe, window.edit_hwnd, expected) == expected

        # modifier combos post without error; live-key-state semantics are a
        # documented caveat of non-intrusive posting, never a silent lie
        combo = adapter.execute("desktop.press_key", {"app_id": "livetest", "key": "ctrl+a"})
        assert combo.ok, combo.error
        assert combo.data["pressed"] is True
        assert combo.data["modifier_caveat"]

        # targeted click resolves the deepest real child at the point
        clicked = adapter.execute(
            "desktop.click", {"app_id": "livetest", "x": 100, "y": 60}
        )
        assert clicked.ok, clicked.error
        assert clicked.data["target_hwnd"] == window.edit_hwnd

        scrolled = adapter.execute(
            "desktop.scroll", {"app_id": "livetest", "x": 100, "y": 60, "clicks": -2}
        )
        assert scrolled.ok, scrolled.error

        # the whole run so far never made our window the foreground window
        assert probe.foreground_hwnd() != window.hwnd

        captured = adapter.execute("desktop.capture", {"app_id": "livetest"})
        assert captured.ok, captured.error
        artifact = Path(captured.data["artifact"]["path"])
        png = artifact.read_bytes()
        _decode_png_chunks(png)  # structurally valid PNG
        assert captured.data["capture_method"].startswith("win32-")
        assert captured.data["quality"]["meaningful"] is True
        assert captured.data["width"] == captured.data["window"]["width"]
        assert captured.data["height"] == captured.data["window"]["height"]

        # a second capture of an unchanged window reports honest freshness
        again = adapter.execute("desktop.capture", {"app_id": "livetest"})
        assert again.ok, again.error
        diagnostics = again.data["capture_diagnostics"]
        assert diagnostics["freshness"] == "repeated_frame" or diagnostics["fallbacks"]

        # background control never stole the user's foreground
        assert probe.foreground_hwnd() == foreground_before

        detached = adapter.execute("desktop.detach", {"app_id": "livetest"})
        assert detached.ok and detached.data["detached"] is True
    finally:
        window.stop()
    assert probe.is_window(window.hwnd) is False or window.hwnd == 0


@pytest.mark.skipif(
    os.environ.get("KAROX_DESKTOP_LIVE_TERMINAL") != "1",
    reason="puts a real visible window on screen; opt in explicitly",
)
def test_live_visible_terminal_is_real(tmp_path: Path) -> None:
    """'Show me the terminal': a real visible console, approval-gated, proven, closed."""

    import psutil

    probe = default_probe()
    adapter = _make_adapter(tmp_path, session="live-terminal")
    payload = {"shell": "cmd", "keep_open": True}

    # without a human approval token the action fails closed
    blocked = adapter.execute("desktop.open_terminal", payload)
    assert blocked.ok is False
    assert blocked.error["type"] == "approval_required"
    digest = action_digest("desktop.open_terminal", payload)
    assert blocked.error["action_digest"] == digest

    # the test plays the human layer: issue the one-shot token out-of-band
    token = adapter.approvals.issue(digest, session_id="live-terminal")
    opened = adapter.execute("desktop.open_terminal", payload, confirmation_token=token)
    assert opened.ok, opened.error
    data = opened.data
    pid = data["process_id"]
    window_id = None
    process = None
    try:
        assert data["visible"] is True
        assert data["window_matched"] is True, "no real window appeared for the terminal launch"
        assert data["window_binding"] in {"process-match", "new-window", "title-match"}
        window_id = data["window"]["window_id"]
        assert probe.is_window(window_id)
        if data["window_binding"] == "process-match":
            assert data["window"]["process_id"] == pid
        # the window's owning process (cmd/conhost, or WindowsTerminal under
        # Win11 default-terminal delegation) is alive
        window_process = psutil.Process(data["window"]["process_id"])
        assert window_process.is_running()

        process = psutil.Process(pid)
        assert process.is_running()
        assert process.create_time() == pytest.approx(
            data["evidence"]["process_identity"]["create_time"], abs=0.01
        )

        # the terminal renders real content through background capture
        attached = adapter.execute(
            "desktop.attach", {"app_id": "liveterm", "window_id": window_id}
        )
        assert attached.ok, attached.error
        shot = adapter.execute("desktop.capture", {"app_id": "liveterm"})
        assert shot.ok, shot.error
        _decode_png_chunks(Path(shot.data["artifact"]["path"]).read_bytes())
        assert shot.data["capture_method"].startswith("win32-")

        # the token was single-use: an immediate replay is refused
        replay = adapter.execute("desktop.open_terminal", payload, confirmation_token=token)
        assert replay.ok is False
        assert replay.error["reason"] == "approval_already_used"
    finally:
        # Close the window gracefully first (it may be a tab inside the user's
        # shared Windows Terminal process — never kill the window owner), then
        # terminate the launched shell process itself.
        if window_id is not None and probe.is_window(window_id):
            probe.post_message(window_id, 0x0010, 0, 0)  # WM_CLOSE
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:  # pragma: no cover
                process.kill()
        if window_id is not None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and probe.is_window(window_id):
                probe.sleep_ms(100)
            assert not probe.is_window(window_id), "terminal window survived cleanup"
