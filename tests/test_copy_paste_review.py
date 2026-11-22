"""Regression tests for the review of copy and paste (dev42): the Win32 clipboard
contract, paste ordering and loss, the Windows key-burst merge, the Linux tools, /copy
arguments and the mouse selection. Nothing here touches the real clipboard."""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from clip_tools import FakeWin32, restore, snapshot
from karox6 import clipboard, tui_app, tui_input
from karox6.providers.scripted import Turn
from karox6.tui_app import FullScreenTui, _mark_selection, _TranscriptControl
from karox6.tui_paste import PasteChunks
from karox6.tui_text import Palette
from test_chat_features import chat
from test_tui_fullscreen import (
    ScreenOutput,
    _mouse,
    eventually,
    make_full_app,
    run_timed,
)

# -- Win32: an owner window, typed calls, clean failure paths, text up to the NUL ----------


@pytest.fixture
def win32(monkeypatch: pytest.MonkeyPatch) -> FakeWin32:
    fake = FakeWin32()
    monkeypatch.setattr(clipboard, "_win32", lambda: fake)
    monkeypatch.setattr(clipboard, "_OPEN_PAUSE", 0)
    return fake


def test_windows_write_opens_the_clipboard_with_an_owner_window(win32: FakeWin32) -> None:
    clipboard._windows_text_set("Привет")
    assert win32.formats[13].decode("utf-16-le") == "Привет\x00"
    assert "OpenClipboard(hwnd)" in win32.log
    assert "OpenClipboard(NULL)" not in win32.log
    assert win32.log.index("CloseClipboard") < win32.log.index("DestroyWindow")
    assert "GlobalFree" not in win32.log  # the clipboard owns the memory now
    assert not win32.is_open
    assert win32.leaked() == set()


@pytest.mark.parametrize("fault", ["open", "empty", "set", "lock", "alloc", "window"])
def test_windows_write_cleans_up_whichever_call_fails(win32: FakeWin32, fault: str) -> None:
    win32.fail.add(fault)
    with pytest.raises(clipboard.ClipboardError):
        clipboard._windows_text_set("x")
    assert not win32.is_open
    assert win32.leaked() == set()


def test_windows_open_is_retried_while_another_program_holds_the_clipboard(
    win32: FakeWin32,
) -> None:
    win32.open_failures = 3
    clipboard._windows_text_set("late")
    assert win32.formats[13].decode("utf-16-le") == "late\x00"


def test_windows_read_ends_at_the_first_nul_and_closes_the_clipboard(win32: FakeWin32) -> None:
    win32.formats[13] = "Привет\0XYZ\0".encode("utf-16-le")
    assert clipboard._windows_text_get() == "Привет"
    win32.formats[13] = b"\0\0\0\0"
    assert clipboard._windows_text_get() == ""
    win32.formats.clear()  # an image-only clipboard has no text
    assert clipboard._windows_text_get() is None
    assert not win32.is_open


def test_windows_picture_read_closes_the_clipboard_even_for_a_damaged_bitmap(
    win32: FakeWin32,
) -> None:
    win32.formats[8] = b"damaged"
    with pytest.raises(clipboard.ClipboardError, match="damaged"):
        clipboard._windows_png()
    assert not win32.is_open
    win32.formats[win32.user32.RegisterClipboardFormatW("PNG")] = b"\x89PNG data"
    assert clipboard._windows_png() == b"\x89PNG data"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 prototypes")
def test_win32_calls_are_typed_for_64_bit_handles_and_not_set_on_the_shared_dll() -> None:
    api = clipboard._win32()
    voidp = ctypes.c_void_p
    for fn in (
        api.user32.OpenClipboard,
        api.user32.GetClipboardData,
        api.user32.SetClipboardData,
        api.user32.CreateWindowExW,
        api.user32.DestroyWindow,
        api.kernel32.GlobalAlloc,
        api.kernel32.GlobalLock,
        api.kernel32.GlobalUnlock,
        api.kernel32.GlobalFree,
        api.kernel32.GlobalSize,
    ):
        assert fn.argtypes is not None, fn.__name__
    assert api.user32.OpenClipboard.argtypes == [voidp]
    assert api.user32.SetClipboardData.restype is voidp
    assert api.kernel32.GlobalLock.restype is voidp
    windll = ctypes.windll  # type: ignore[attr-defined]
    assert windll.user32.SetClipboardData.argtypes is None


def test_snapshot_and_restore_give_back_text_and_picture(win32: FakeWin32) -> None:
    """The tools the opt-in real-clipboard test relies on, proven on the fake: whatever
    the test writes in between, the clipboard ends as it was, formats and all."""
    clipboard._windows_text_set("my own text")
    png = win32.user32.RegisterClipboardFormatW("PNG")
    owner, ours = clipboard._owner_window(win32)
    clipboard._open_clipboard(win32.user32, owner)
    handle = win32.kernel32.GlobalAlloc(0x42, 6)
    ctypes.memmove(win32.kernel32.GlobalLock(handle), b"\x89PNGxx", 6)
    win32.user32.SetClipboardData(png, handle)
    win32.user32.CloseClipboard()
    win32.user32.DestroyWindow(owner)
    assert ours
    before = dict(win32.formats)
    saved = snapshot(win32)
    assert {fmt for fmt, _ in saved} == {13, png}
    clipboard._windows_text_set("karox6 test text")  # what the real test writes in between
    assert win32.formats != before
    restore(win32, saved)
    assert win32.formats == before
    restore(win32, [])  # an empty clipboard comes back empty
    assert win32.formats == {}
    assert win32.leaked() == set()


# -- Linux tools -----------------------------------------------------------------------


def test_wl_paste_asks_for_text_and_the_next_tool_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str]) -> bytes | None:
        calls.append(command)
        return None if command[0] == "wl-paste" else b"from xclip"

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: name)
    monkeypatch.setattr(clipboard, "_run", run)
    assert clipboard.grab_text() == "from xclip"
    assert calls[0] == ["wl-paste", "--type", "text", "--no-newline"]
    assert calls[1][0] == "xclip"
    monkeypatch.setattr(clipboard, "_run", lambda _c: None)
    assert clipboard.grab_text() is None  # tools there, nothing as text on the clipboard


def test_clipboard_tools_never_leave_us_waiting_on_a_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, dict[str, object]] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen[command[0]] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    clipboard._pipe(["wl-copy"], b"x")
    clipboard._pipe(["xclip", "-selection", "clipboard"], b"x")
    assert seen["wl-copy"]["stderr"] is subprocess.DEVNULL
    assert seen["xclip"]["stderr"] is not subprocess.PIPE
    for kwargs in seen.values():
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert not kwargs.get("capture_output")


def test_a_tool_whose_background_server_holds_our_pipes_does_not_block_copy() -> None:
    """wl-copy and xclip fork a server that outlives them with the same output handles;
    waiting for those handles to close would wait for the server."""
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], close_fds=False)\n"
    )
    started = time.monotonic()
    clipboard._pipe([sys.executable, "-c", script], b"text")
    assert time.monotonic() - started < 2.0


# -- /copy arguments ---------------------------------------------------------------------


@pytest.mark.parametrize("arg", ["²", "-1", "9" * 5000, "1.5", "٣", "1 2", "0x1"])
def test_copy_with_a_strange_argument_shows_usage_and_the_chat_goes_on(
    tmp_path: Path, arg: str
) -> None:
    copied: list[str] = []
    _, _, term, _ = chat(
        tmp_path,
        [Turn(text="an answer")],
        ["hi", f"/copy {arg}", "/copy", "/quit"],
        copy_to_clipboard=lambda text: copied.append(text) or "fake",
    )
    assert "usage: /copy" in term.text
    assert copied == ["an answer"]  # the plain /copy after it still works


# -- placeholders of long pastes -----------------------------------------------------------


def test_expand_is_one_pass_a_pasted_label_stays_literal() -> None:
    pastes = PasteChunks()
    first = "\n".join(f"first {i}" for i in range(30))
    label = pastes.offer(first)
    second = "\n".join(f"second {i}" for i in range(1000)) + f"\nmentions {label}"
    other = pastes.offer(second)
    assert other != label
    assert pastes.expand(f"{label} then {other}") == f"{first} then {second}"


def test_a_short_paste_spelling_a_live_label_stays_literal() -> None:
    pastes = PasteChunks()
    label = pastes.offer("\n".join(f"first {i}" for i in range(30)))
    shown = pastes.offer(f"see {label}")  # small, but it contains a label of this draft
    assert shown != f"see {label}"
    assert pastes.expand(shown) == f"see {label}"


# -- the Windows key-burst merge ------------------------------------------------------------


def _presses(text: str) -> list[KeyPress]:
    out = []
    for char in text:
        if char == "\n":
            out.append(KeyPress(Keys.ControlJ, "\n"))
        elif char == "\r":
            out.append(KeyPress(Keys.ControlM, "\r"))
        else:
            out.append(KeyPress(char, char))
    return out


class _ScriptedInput:
    """An Input stand-in: ``read_keys`` hands out the next scripted batch."""

    def __init__(self, *batches: str) -> None:
        self.batches = [_presses(b) for b in batches]
        self.closed = False

    def read_keys(self) -> list[KeyPress]:
        return self.batches.pop(0) if self.batches else []

    def flush_keys(self) -> list[KeyPress]:
        return []


def _summary(keys: list[KeyPress]) -> list[tuple[object, str | None]]:
    return [(p.key, p.data) for p in keys]


def test_a_literal_end_marker_inside_a_paste_loses_and_reorders_nothing() -> None:
    merge = tui_input.merge_pasted_runs
    keys = _presses("abc[201~def\r")
    out, _ = merge(keys, hot=False)
    assert out == keys  # short typing: untouched, in order
    out, _ = merge(_presses("abc[201~def\r"), hot=True)
    assert [p.data for p in out] == ["abc[201~def\r"]  # one paste, text intact
    # the real marker, with the Enter that submits after it
    out, still_open = merge(_presses("x" * 40 + "[201~\r"), hot=False)
    assert _summary(out) == [(Keys.BracketedPaste, "x" * 40), (Keys.ControlM, "\r")]
    assert not still_open


def test_an_end_marker_cut_by_a_read_boundary_is_not_left_in_the_text() -> None:
    inner = _ScriptedInput("x" * 40 + "[20", "1~\r")
    wrapped = tui_input._CoalescingInput(inner)  # type: ignore[arg-type]
    assert wrapped.read_keys() == []  # the paste waits: its end may be in the next read
    out = wrapped.read_keys()
    assert _summary(out) == [(Keys.BracketedPaste, "x" * 40), (Keys.ControlM, "\r")]


def test_a_paste_waits_for_the_quiet_flush_and_a_held_prefix_is_released() -> None:
    inner = _ScriptedInput("y" * 40)
    wrapped = tui_input._CoalescingInput(inner)  # type: ignore[arg-type]
    assert wrapped.read_keys() == []
    assert _summary(wrapped.flush_keys()) == [(Keys.BracketedPaste, "y" * 40)]
    typed = tui_input._CoalescingInput(_ScriptedInput("ab[2"))  # type: ignore[arg-type]
    assert typed.read_keys() == _presses("ab")  # "[2" might start a marker: held
    assert typed.flush_keys() == _presses("[2")  # nothing followed: it was typing
    bracket = tui_input._CoalescingInput(_ScriptedInput("a["))  # type: ignore[arg-type]
    assert bracket.read_keys() == _presses("a[")  # a lone bracket never waits


def test_the_paste_end_marker_also_ends_the_hot_window() -> None:
    inner = _ScriptedInput("x" * 40, "tail[201~", "\r")  # a paste over two reads, then Enter
    wrapped = tui_input._CoalescingInput(inner)  # type: ignore[arg-type]
    assert wrapped.read_keys() == []
    assert _summary(wrapped.read_keys()) == [(Keys.BracketedPaste, "x" * 40 + "tail")]
    assert _summary(wrapped.read_keys()) == [(Keys.ControlM, "\r")]  # submits at once


def test_conpty_hands_the_markers_over_as_an_escape_press_and_it_goes_with_them() -> None:
    """The Escape would wait to join the next key: an Enter right after a paste would
    arrive as Alt+Enter, a new line, instead of sending the message."""
    esc = KeyPress(Keys.Escape, "\x1b")
    inner = _ScriptedInput()
    inner.batches = [[*_presses("x" * 40), esc, *_presses("[201~")], _presses("\r")]
    wrapped = tui_input._CoalescingInput(inner)  # type: ignore[arg-type]
    assert _summary(wrapped.read_keys()) == [(Keys.BracketedPaste, "x" * 40)]
    assert _summary(wrapped.read_keys()) == [(Keys.ControlM, "\r")]
    head = [esc, *_presses("[200~a\nb\nc")]
    assert _summary(tui_input.merge_pasted_runs(head, hot=False)[0]) == [
        (Keys.BracketedPaste, "a\nb\nc")
    ]
    assert tui_input.merge_pasted_runs([esc, *_presses("hi")], hot=False)[0] == [
        esc,
        *_presses("hi"),
    ]  # a real Escape stays a key


def test_a_paste_is_handed_over_before_the_key_that_follows_it() -> None:
    inner = _ScriptedInput("z" * 40)
    wrapped = tui_input._CoalescingInput(inner)  # type: ignore[arg-type]
    assert wrapped.read_keys() == []
    inner.batches.append([KeyPress(Keys.ControlC, "\x03")])
    out = wrapped.read_keys()
    assert _summary(out) == [(Keys.BracketedPaste, "z" * 40), (Keys.ControlC, "\x03")]


def test_the_burst_heuristics_are_for_the_windows_console_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = _ScriptedInput()
    monkeypatch.setattr(tui_input, "_windows_console", lambda: False)
    assert tui_input.coalescing_input(inner) is inner  # type: ignore[arg-type]
    monkeypatch.setattr(tui_input, "_windows_console", lambda: True)
    assert tui_input.coalescing_input(inner) is not inner  # type: ignore[arg-type]


# -- the draft in the full screen: ordering, cancelling, slow clipboards ---------------------


class SlowClipboard:
    """A ``grab_text`` that waits for the test to let it go."""

    def __init__(self, *texts: str | None) -> None:
        self.texts = list(texts)
        self.gate = threading.Event()
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        self.gate.wait(10)
        return self.texts.pop(0)


def _ui(tmp_path: Path, keys: object, out: ScreenOutput | None = None) -> FullScreenTui:
    return FullScreenTui(
        workspace=tmp_path / "ws",
        home=tmp_path / "home",
        input=keys,  # type: ignore[arg-type]
        output=out or ScreenOutput(),
        complete_in_thread=False,
    )


def test_typing_around_a_paste_stays_in_order(tmp_path: Path) -> None:
    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, provider = make_full_app(tmp_path, [Turn(text="one"), Turn(text="two")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("A\x1b[200~B\x1b[201~C\r")
                await eventually(lambda: len(provider.requests) == 1)
                assert provider.requests[0].messages[-1].content == "ABC"
                await eventually(lambda: ui.mode == "prompt")
                big = "\n".join(f"row {i}" for i in range(50))
                keys.send_text("A\x1b[200~" + big + "\x1b[201~C\r")
                await eventually(lambda: len(provider.requests) == 2)
                assert provider.requests[1].messages[-1].content == f"A{big}C"
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


def test_a_paste_split_in_the_middle_of_a_crlf_has_one_line_end(tmp_path: Path) -> None:
    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            async with ui:
                await asyncio.sleep(0.05)
                keys.send_text("\x1b[200~line\r\x1b[201~\x1b[200~\n\ttext\x1b[201~")
                await eventually(lambda: "text" in ui.input_buffer.text)
                assert ui.input_buffer.text == "line\n\ttext"

    run_timed(go())


def test_ctrl_c_drops_the_pastes_of_the_cancelled_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged paste still waiting, a clipboard read still running, a placeholder:
    none of them survives into the next draft."""
    monkeypatch.setattr(tui_input, "_windows_console", lambda: True)
    slow = SlowClipboard("late clipboard text")
    monkeypatch.setattr(clipboard, "grab_text", slow)

    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, provider = make_full_app(tmp_path, [Turn(text="ok")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("A" + "x" * 40 + "\x03")  # a burst, then Ctrl+C
                await eventually(lambda: ui.input_buffer.text == "")
                await asyncio.sleep(0.4)  # longer than the paste's hold
                assert ui.input_buffer.text == ""
                keys.send_text("B\x16")  # a slow Ctrl+V ...
                await eventually(lambda: slow.calls == 1)
                keys.send_text("\x03")  # ... and Ctrl+C before it answers
                await eventually(lambda: ui.input_buffer.text == "")
                slow.gate.set()
                await asyncio.sleep(0.3)
                assert ui.input_buffer.text == ""
                # a placeholder typed by hand after Ctrl+C is only text
                big = "\n".join(f"row {i}" for i in range(50))
                keys.send_text("\x1b[200~" + big + "\x1b[201~")
                await eventually(lambda: "[pasted" in ui.input_buffer.text)
                label = ui.input_buffer.text
                keys.send_text("\x03")
                await eventually(lambda: ui.input_buffer.text == "")
                keys.send_text(label + "\r")
                await eventually(lambda: len(provider.requests) == 1)
                assert provider.requests[0].messages[-1].content == label
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


def test_enter_waits_for_a_running_clipboard_read_and_two_ctrl_v_both_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowClipboard("[one]", "[two]")
    monkeypatch.setattr(clipboard, "grab_text", slow)

    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, provider = make_full_app(tmp_path, [Turn(text="ok")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("A\x16\x16C\r")  # A, two Ctrl+V, C, Enter
                await eventually(lambda: slow.calls == 1)
                await asyncio.sleep(0.3)
                assert provider.requests == []  # the Enter is waiting for the clipboard
                assert ui.input_buffer.text == "AC"
                slow.gate.set()
                await eventually(lambda: len(provider.requests) == 1)
                # both texts, in order, where the keys were pressed
                assert provider.requests[0].messages[-1].content == "A[one][two]C"
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


def test_a_pasted_label_of_an_earlier_paste_stays_as_pasted(tmp_path: Path) -> None:
    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, provider = make_full_app(tmp_path, [Turn(text="ok")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                first = "\n".join(f"first {i}" for i in range(30))
                keys.send_text("\x1b[200~" + first + "\x1b[201~")
                await eventually(lambda: "[pasted" in ui.input_buffer.text)
                label = ui.input_buffer.text
                second = "\n".join(f"second {i}" for i in range(1000)) + f"\nsee {label}"
                keys.send_text("\x1b[200~" + second + "\x1b[201~")
                await eventually(lambda: ui.input_buffer.text.count("[pasted") == 2)
                keys.send_text("\r")
                await eventually(lambda: len(provider.requests) == 1)
                assert provider.requests[0].messages[-1].content == first + second
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


def test_an_empty_bracketed_paste_reads_the_clipboard_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowClipboard("never read")
    slow.gate.set()
    monkeypatch.setattr(clipboard, "grab_text", slow)
    monkeypatch.setattr(tui_app, "_windows_terminal", lambda: False)

    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, _ = make_full_app(tmp_path, [], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("\x1b[200~\x1b[201~")
                await asyncio.sleep(0.4)
                assert slow.calls == 0
                assert ui.input_buffer.text == ""
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


def test_leaving_does_not_wait_for_a_clipboard_thread_that_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    seen: list[str] = []

    def blocked() -> None:
        seen.append("reading")
        gate.wait(6)

    monkeypatch.setattr(clipboard, "grab_text", blocked)

    async def go() -> None:
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys)
            app, _ = make_full_app(tmp_path, [], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("\x16")
                await eventually(lambda: bool(seen))
                keys.send_text("\x04")  # Ctrl+D on an empty line leaves
                assert await asyncio.wait_for(running, 3) == 0

    started = time.monotonic()
    try:
        run_timed(go())
        assert time.monotonic() - started < 3.0  # asyncio.run did not wait for the thread
    finally:
        gate.set()


def test_osc52_goes_out_from_the_event_loop_and_no_copy_loses_its_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()

    def fake_copy(text: str, *, emit_osc52: object = None) -> str:
        if text == "slow":
            gate.wait(5)
        emit_osc52(clipboard.osc52(text))  # type: ignore[operator]
        return "osc52"

    monkeypatch.setattr(clipboard, "copy_text", fake_copy)
    writers: list[int] = []

    class ThreadedOutput(ScreenOutput):
        def write_raw(self, data: str) -> None:
            if data.startswith("\x1b]52"):
                writers.append(threading.get_ident())
            super().write_raw(data)

    async def go() -> None:
        out = ThreadedOutput()
        with create_pipe_input() as keys:
            ui = _ui(tmp_path, keys, out)
            async with ui:
                ui._copy_selection("slow")
                await asyncio.sleep(0.05)
                ui._copy_selection("fast")
                await eventually(lambda: clipboard.osc52("fast") in out.text)
                gate.set()
                await eventually(lambda: clipboard.osc52("slow") in out.text)
                assert set(writers) == {threading.get_ident()}

    try:
        run_timed(go())
    finally:
        gate.set()


# -- the mouse selection ---------------------------------------------------------------------


def _drag(t: _TranscriptControl, start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    """Press at (row, character), drag, release: what the pane copies. The mouse names
    the character it is over, a spot past the end of a row names the last one."""
    got: list[str] = []
    t.on_copy = got.append
    t.create_content(40, 10)
    t.mouse_handler(_mouse(start[1], start[0], MouseEventType.MOUSE_DOWN, MouseButton.LEFT))
    t.mouse_handler(_mouse(end[1], end[0], MouseEventType.MOUSE_MOVE, MouseButton.LEFT))
    t.mouse_handler(_mouse(end[1], end[0], MouseEventType.MOUSE_UP, MouseButton.LEFT))
    return got


def test_a_drag_to_the_end_of_a_row_takes_the_last_character_wide_or_not() -> None:
    t = _TranscriptControl()
    t.append("A界\nplain row\n")
    assert _drag(t, (0, 0), (0, 1)) == ["A界"]  # 界 fills two cells, one character
    assert _drag(t, (1, 0), (1, 11)) == ["plain row"]
    assert _drag(t, (1, 0), (1, 4)) == ["plai"]  # inside a row the end is exclusive


def test_a_selection_keeps_real_spaces_and_box_characters() -> None:
    t = _TranscriptControl()
    t.append("a │ b ─ c  \nnext\n")
    assert _drag(t, (0, 0), (1, 2)) == ["a │ b ─ c  \nne"]
    t = _TranscriptControl()
    t.append("x  \n\n  y\n")
    assert _drag(t, (0, 0), (2, 2)) == ["x  \n\n  y"]


def test_only_what_the_chat_marked_as_decoration_is_left_out_of_a_copy() -> None:
    paint = Palette(True)
    paint.mark_frames = True
    t = _TranscriptControl()
    t.append(paint.frame("│ ") + "quoted │ text\n" + paint.frame("─" * 5) + "\nbox │ ─ kept\n")
    assert "\ue001" not in t.plain_text  # the marks are consumed, never shown
    assert _drag(t, (0, 0), (2, 11)) == ["quoted │ text\n\nbox │ ─ kept"]
    plain = Palette(True)  # a chat that does not run the full screen marks nothing
    assert plain.frame("│ ") == plain("subtle", "│ ")


def test_the_highlight_of_a_row_made_of_several_fragments_is_bounded() -> None:
    sel = (0, 0, 0, 1)  # the first character only
    line = [("red", "ab"), ("blue", "cdef")]
    marked = _mark_selection(line, 0, sel)
    assert "".join(text for _, text in marked) == "abcdef"
    assert [(s, t) for s, t in marked if "selected" in s] == [("red class:selected", "a")]
    far = _mark_selection(line, 0, (0, 5, 0, 6))  # the last character only
    assert [t for s, t in far if "selected" in s] == ["f"]
    assert "".join(text for _, text in far) == "abcdef"


def test_a_selection_is_copied_as_it_was_when_the_drag_began_not_as_it_streams() -> None:
    t = _TranscriptControl()
    t.create_content(40, 10)
    t.set_live("**foo")  # the streaming line, before its closing marks arrive
    t.create_content(40, 10)
    row = len(t._visual_lines)
    got: list[str] = []
    t.on_copy = got.append
    t.mouse_handler(_mouse(2, row, MouseEventType.MOUSE_DOWN, MouseButton.LEFT))
    t.set_live("\x1b[1mfoo\x1b[0m")  # the line re-formats under the mouse: "**" is gone
    t.create_content(40, 10)
    t.mouse_handler(_mouse(4, row, MouseEventType.MOUSE_MOVE, MouseButton.LEFT))
    t.mouse_handler(_mouse(4, row, MouseEventType.MOUSE_UP, MouseButton.LEFT))
    assert got == ["foo"]


def test_a_new_wrap_width_drops_a_selection_whose_rows_changed_meaning() -> None:
    t = _TranscriptControl()
    t.append("some text here\n")
    t.create_content(40, 10)
    t.mouse_handler(_mouse(0, 0, MouseEventType.MOUSE_DOWN, MouseButton.LEFT))
    t.mouse_handler(_mouse(5, 0, MouseEventType.MOUSE_MOVE, MouseButton.LEFT))
    assert t.selection_text() == "some "
    t.create_content(10, 10)
    assert t.selection_text() == ""


def test_a_real_mouse_drag_to_the_blank_right_of_a_wide_row_copies_all_of_it(
    tmp_path: Path,
) -> None:
    """Through prompt_toolkit's own mouse handling, which names the last character for
    any spot past the end of a row."""
    copied: list[str] = []

    async def go() -> None:
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=ScreenOutput(),
                complete_in_thread=False,
                copy_to_clipboard=lambda text: copied.append(text) or "fake",
            )
            async with ui:
                ui.write("A界\n")
                await eventually(lambda: any("A界" in r for r in _rows(ui)))
                row = next(i for i, r in enumerate(_rows(ui)) if "A界" in r)
                y = row + 1  # terminal coordinates count from 1
                keys.send_text(f"\x1b[<0;1;{y}M\x1b[<32;30;{y}M\x1b[<0;30;{y}m")
                await eventually(lambda: bool(copied))

    run_timed(go())
    assert copied == ["A界"]


def _rows(ui: FullScreenTui) -> list[str]:
    from test_tui_fullscreen import rendered_rows

    return rendered_rows(ui)
