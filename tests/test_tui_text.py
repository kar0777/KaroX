"""Pure text of the chat UI: the welcome card, the status bar, error explanations and
the start-up frame."""

import atexit
import io
import os
import sys
from pathlib import Path

import pytest

from karox6 import splash
from karox6.core.digest import error_detail, explain_error
from karox6.tui_text import (
    _ANSI,
    Palette,
    StatusSnapshot,
    _box,
    _glyphs,
    banner_text,
    cell_width,
    clip_cells,
    narrow,
    status_bar_fragments,
)

GLYPH = _glyphs("utf-8")


def snapshot(**changes):
    values = {
        "model": "deepseek-v4.1-flash",
        "provider": "openai-chat",
        "workspace_name": "project",
        "branch": "main",
        "approval_mode": "ask",
        "running": False,
        "activity": "",
        "total_tokens": 12_300,
        "context_used": 4_200,
        "context_window": 400_000,
        "context_pct": 1,
        "session_id": None,
    }
    values.update(changes)
    return StatusSnapshot(**values)


def bar(width, **changes):
    extra = {k: changes.pop(k) for k in ("scrolled", "queued") if k in changes}
    return "".join(t for _, t in status_bar_fragments(snapshot(**changes), GLYPH, width, **extra))


def test_status_bar_fills_the_width_exactly():
    for width in (200, 120, 80, 60, 45, 30, 22):
        for changes in (
            {},
            {"running": True, "activity": "| 12.3s  Running bash, read"},
            {"approval_mode": "allow", "branch": ""},
            {"scrolled": 40},
            {"model": "anthropic/claude-something-very-long-name-preview-2026"},
            {"workspace_name": "проект", "branch": "ветка/фича"},
        ):
            assert cell_width(bar(width, **changes)) == width, (width, changes)


def test_narrow_status_bar_keeps_the_model_and_the_approval_mode():
    wide = bar(120)
    assert "12.3k tok" in wide
    assert "project (main)" in wide
    assert "ctx 4.2k/400.0k (1%)" in wide
    # the least telling parts go first: tokens, branch, project, context fill
    assert "tok" not in bar(80)
    assert "project (main)" in bar(80)
    assert "(main)" not in bar(70)
    assert "project" in bar(70)
    assert "project" not in bar(60)
    assert "ctx 4.2k/400.0k (1%)" in bar(60)
    assert "ctx" not in bar(48)
    for width in (80, 70, 60, 48, 36):
        text = bar(width)
        assert "deepseek-v4.1-flash" in text
        assert text.rstrip().endswith("approve:ask")  # was cut off below ~60 columns
    # even a tiny window still says who answers and whether commands need approval
    tiny = bar(24)
    assert tiny.rstrip().endswith("approve:ask")
    assert tiny.strip().startswith("deep")


def test_status_bar_activity_scroll_and_queue_notes():
    running = bar(100, running=True, activity="/ 3.2s  Model thinking", queued=2)
    assert "/ 3.2s  Model thinking" in running
    assert "2 queued (Esc clears)" in running
    assert "scrolled +15L (End)" in bar(100, scrolled=15)
    assert "scrolled" not in bar(100, running=True, activity="x", scrolled=15)
    fragments = status_bar_fragments(snapshot(approval_mode="deny"), GLYPH, 100)
    assert ("deny", "approve:deny") in fragments


def test_cells_and_clipping():
    assert narrow("plain ascii · кириллица ─│●✓✗›…")
    assert not narrow("日本語")
    assert not narrow("emoji 🙂")
    assert cell_width("abc") == 3
    assert cell_width("日本語") == 6
    assert clip_cells("abcdef", 4) == "abc…"
    assert clip_cells("abc", 4) == "abc"
    assert cell_width(clip_cells("日本語日本語", 5)) <= 5
    assert clip_cells("abc", 0) == ""


def test_the_welcome_card_never_wraps_on_a_narrow_terminal():
    paint = Palette(True)
    for width in (120, 80, 50, 30):
        card = banner_text(
            version="6.0.0",
            model="some-model",
            provider="openai-chat",
            workspace="C:/Users/someone/projects/a-project-with-a-long-name/sub/folder",
            branch="feature/long-branch-name",
            approval_mode="ask",
            paint=paint,
            glyph=GLYPH,
            max_width=width,
        )
        lines = [_ANSI.sub("", line) for line in card.rstrip("\n").split("\n")]
        assert max(cell_width(line) for line in lines) <= width
        box = lines[:-1]
        assert len({cell_width(line) for line in box}) == 1  # the box stays a box
        assert box[0].startswith("╭")
        assert box[-1].startswith("╰")
    # without a limit nothing is cut
    assert "v" * 300 in _box("T", [("k", "v" * 300)], True)


def frame_args(**changes):
    values = {
        "version": "6.0.0",
        "model": "some-model",
        "provider": "openai-chat",
        "workspace": Path.cwd(),
        "approval_mode": "ask",
        "context_window": 400_000,
        "encoding": "utf-8",
        "rows": 30,
        "columns": 100,
    }
    values.update(changes)
    return values


def test_first_frame_puts_card_bar_and_prompt_where_the_app_draws_them():
    frame = splash.first_frame(**frame_args())
    assert frame is not None
    assert frame.startswith(splash.ENTER_ALTERNATE + "\x1b[H\x1b[2J")
    assert frame.count(splash.ENTER_ALTERNATE) == 1
    assert splash.LEAVE_ALTERNATE not in frame
    assert "Type a task to start." in frame
    bar_row, prompt_row = frame.index("\x1b[29;1H"), frame.index("\x1b[30;1H")
    assert bar_row < frame.index("approve:ask") < prompt_row  # bar: the last row but one
    assert frame.endswith(GLYPH["prompt"] + "\x1b[0m")  # prompt: the last row


@pytest.mark.parametrize(
    "changes",
    [
        {"rows": 10},  # the card would not fit above the bar and the prompt
        {"columns": 15},
        {"model": "模型"},  # widths that need tables: the app draws it itself
        {"workspace": Path("C:/проекты/日本語")},
    ],
)
def test_first_frame_is_not_painted_when_it_could_not_be_exact(changes):
    assert splash.first_frame(**frame_args(**changes)) is None


def test_the_painted_frame_is_given_back_exactly_once(monkeypatch):
    written = []
    monkeypatch.setattr(splash, "_vt_write", lambda _stream, text: written.append(text) or True)
    shown = splash.Splash(io.StringIO())
    assert shown.take_over() is True  # the app takes the alternate screen as it is
    assert shown.take_over() is False
    shown.close()
    assert written == []  # the app leaves the screen itself
    # the app never started: the splash leaves the alternate screen, once
    shown = splash.Splash(io.StringIO())
    shown.close()
    shown.close()
    assert written == [splash.LEAVE_ALTERNATE]


@pytest.mark.skipif(sys.platform == "win32", reason="the line mode of a POSIX terminal")
def test_keys_typed_under_the_first_frame_stay_as_typed():
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        found = termios.tcgetattr(slave)
        assert found[3] & termios.ECHO  # a new terminal: line mode, echo, CR read as LF
        assert found[3] & termios.ICANON
        assert found[0] & termios.ICRNL
        restore = splash._quiet_input(slave)
        assert restore is not None
        quiet = termios.tcgetattr(slave)
        assert not quiet[3] & (termios.ECHO | termios.ICANON)
        assert not quiet[0] & termios.ICRNL
        assert quiet[3] & termios.ISIG  # Ctrl+C still interrupts the start
        os.write(master, b"hello\r")
        assert os.read(slave, 64) == b"hello\r"  # at once, and Enter is still Enter
        os.set_blocking(master, False)
        with pytest.raises(BlockingIOError):  # nothing was echoed over the frame
            os.read(master, 64)
        restore()
        assert termios.tcgetattr(slave) == found
    finally:
        os.close(master)
        os.close(slave)


def test_input_mode_is_left_alone_where_it_cannot_be_read():
    # not a terminal (or Windows, where a console queues key events as they are)
    with open(os.devnull, "rb") as not_a_terminal:
        assert splash._quiet_input(not_a_terminal.fileno()) is None


def test_the_input_mode_comes_back_on_every_close(monkeypatch):
    monkeypatch.setattr(splash, "_vt_write", lambda _stream, _text: True)
    restored = []
    shown = splash.Splash(io.StringIO(), lambda: restored.append(True))
    assert shown.take_over() is True
    assert restored == []  # the app reads keys in the mode the frame set
    shown.close()  # the app has ended and put back the mode it found: the frame's
    shown.close()  # and again at exit
    assert restored == [True, True]


class _Window(io.StringIO):
    def fileno(self) -> int:
        return 1


@pytest.mark.parametrize("written", [True, False])
def test_show_sets_the_input_mode_only_for_a_painted_frame(monkeypatch, written):
    for name in ("NO_COLOR", "KAROX_NO_SPLASH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(splash, "_is_terminal", lambda _stream: True)
    monkeypatch.setattr(splash.os, "get_terminal_size", lambda _fd: os.terminal_size((100, 30)))
    monkeypatch.setattr(splash, "_vt_write", lambda _stream, _text: written)
    restored = []
    monkeypatch.setattr(splash, "_quiet_input", lambda: lambda: restored.append(True))
    args = frame_args()
    del args["encoding"], args["rows"], args["columns"]
    shown = splash.show(**args, stream=_Window())
    if not written:  # the terminal took no escape sequences: everything as it was
        assert shown is None
        assert restored == [True]
        return
    assert shown is not None
    try:
        assert restored == []
        shown.close()
        assert restored == [True]
    finally:
        atexit.unregister(shown.close)


@pytest.mark.parametrize(
    ("message", "line", "hint"),
    [
        (
            'auth (HTTP 401): {"error": {"message": "Incorrect API key provided: sk-***"}}',
            "the API key was refused (HTTP 401): Incorrect API key provided: sk-***",
            "/key",
        ),
        (
            "transport: connection failed: ConnectError('All connection attempts failed')",
            "cannot reach the model server: All connection attempts failed",
            "/setup",
        ),
        (
            'bad_request (HTTP 404): {"error": {"message": "The model `x` does not exist"}}',
            "the model name was not accepted (HTTP 404): The model `x` does not exist",
            "/model",
        ),
        (
            'quota (HTTP 429): {"error":{"message":"Daily limit of 200 requests reached"}}',
            "the account is out of balance or allowance (HTTP 429): Daily limit of 200 "
            "requests reached",
            "waiting will not help",
        ),
        (
            'context_window (HTTP 400): {"error":{"message":"maximum context length is 8192"}}',
            "the conversation is larger than the model accepts (HTTP 400): maximum context "
            "length is 8192",
            "/compact",
        ),
        (
            "empty_response: the model returned no text and no tool calls",
            "the model returned nothing",
            "/model",
        ),
        (
            "timeout: request timed out: ReadTimeout('')",
            "the model did not answer in time",
            "again",
        ),
    ],
)
def test_provider_errors_read_as_a_sentence_with_a_next_step(message, line, hint):
    shown, advice = explain_error(message)
    assert shown == line
    assert hint in advice


def test_error_text_that_is_not_a_provider_error_is_left_alone():
    plain = "output repeatedly cut off at the token limit"
    assert explain_error(plain) == (plain, "")
    assert explain_error("time limit 3550s") == ("time limit 3550s", "")


def test_error_detail_survives_cut_json_html_and_gateway_prefixes():
    cut = '{"error": {"message": "Rate limit reached for model", "type": "rate_li'
    assert error_detail(cut) == "Rate limit reached for model"
    html = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    assert error_detail(html) == "502 Bad Gateway nginx"
    wrapped = (
        '{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
        'Too many requests. Received Model Group=alias"}}'
    )
    assert error_detail(wrapped) == "Too many requests."
    assert error_detail('{"detail": "Not Found"}') == "Not Found"
    assert error_detail('{"error": "plain string"}') == "plain string"
