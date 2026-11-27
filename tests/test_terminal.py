"""The chat as a user meets it: the real `karox6` process in a real pseudo-terminal.

Each test starts the CLI in a ConPTY (Windows) or a pty (POSIX), types keys the way a
terminal sends them and reads the screen through a terminal emulator (tests/term.py). The
model is a scripted local server (tests/fake_model.py): no network, no key.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyte")
if sys.platform == "win32":
    pytest.importorskip("winpty")

from fake_model import FakeModel, Reply
from term import Terminal, ctrl_c_hook

SRC = Path(__file__).resolve().parents[1] / "src"
ENTER_ALT, LEAVE_ALT = "\x1b[?1049h", "\x1b[?1049l"
PUSH = ("bash", {"command": "git push --force origin main"})  # needs approval (git.push-force)
LINE_MODE = {"echo": True, "line": True, "cr_as_lf": True}  # a POSIX terminal as a shell has it
# What an interrupt left on the user's screen when Python's own handler raised it somewhere
# in the start-up imports.
NOISE = ("Traceback", "Exception ignored", "KeyboardInterrupt", "never awaited")
_ACTIVITY = re.compile(r"\d+\.\ds · esc stop")


@dataclass
class Chat:
    term: Terminal
    model: FakeModel
    ws: Path
    home: Path

    def say(self, text: str, expect: str, timeout: float = 30.0) -> str:
        self.term.line(text)
        return self.term.wait(expect, timeout)

    def idle(self) -> bool:
        """No turn is running: the activity line shows no "12.3s · esc stop"."""
        return not any(_ACTIVITY.search(line) for line in self.term.lines()[-6:])

    def turn(self, text: str, timeout: float = 30.0) -> str:
        """Send a message and wait until its turn has ended (the model was asked and the
        status bar is quiet again)."""
        asked = len(self.model.requests)
        self.term.line(text)
        self.term.wait(lambda _s: len(self.model.requests) > asked and self.idle(), timeout)
        return self.term.text()

    def last_user_message(self) -> object:
        return self.model.requests[-1]["messages"][-1]["content"]


def typed(term: Terminal) -> list[str]:
    """The lines inside the input box (between its top edge and its lower edge), without
    the frame: what the user has typed, after the prompt."""
    lines = term.lines()
    top = max(i for i, line in enumerate(lines) if line.startswith("╭"))
    bottom = max(i for i, line in enumerate(lines) if line.startswith("╰"))
    return [line[2:].rstrip().removesuffix("│").rstrip() for line in lines[top + 1 : bottom]]


def status(term: Terminal) -> str:
    return term.lines()[-1]


def activity(term: Terminal) -> str:
    return term.lines()[-5]


class Launcher:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.ws = tmp_path / "ws"
        self.home = tmp_path / "home"
        self.ws.mkdir()
        (self.ws / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "заметки.md").write_text("# Заметки\nтекст\n", encoding="utf-8")
        self.opened: list[tuple[Terminal, FakeModel]] = []

    def git_init(self) -> None:
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(
                ["git", *args],
                cwd=self.ws,
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KAROX_", "NO_COLOR"))}
        env.pop("PYTHONUTF8", None)
        env.update(
            KAROX_HOME=str(self.home),
            KAROX_API_KEY="test-key",
            PYTHONPATH=str(SRC),
            TERM="xterm-256color",
        )
        env.update(extra)
        return env

    def interruptible(self, at: str = "") -> dict[str, str]:
        """The environment of a chat that takes Ctrl+C as a user's does (term.ctrl_c_hook);
        with ``at`` ("<name>:<how>") it interrupts itself at that point of its start."""
        path = os.pathsep.join([ctrl_c_hook(self.tmp / "hook"), str(SRC)])
        return self.env(PYTHONPATH=path, **({"TEST_SIGINT_AT": at} if at else {}))

    def start(
        self,
        replies: list[Reply],
        *,
        args: tuple[str, ...] = (),
        model: bool = True,
        rows: int = 30,
        cols: int = 100,
        ready: str | None = "Type a task to start.",
        env: dict[str, str] | None = None,
        watch: tuple[str, ...] = (),
    ) -> Chat:
        fake = FakeModel(replies)
        fake.__enter__()
        argv = [sys.executable, "-m", "karox6", "chat", "--home", str(self.home)]
        argv += ["--cwd", str(self.ws), "--no-mcp", *args]
        if model:
            argv += ["--model", "fake", "--base-url", fake.url]
        term = Terminal(
            argv, rows=rows, cols=cols, env=env or self.env(), cwd=str(self.ws), watch=watch
        )
        self.opened.append((term, fake))
        if ready:
            term.wait(ready, 30)
        return Chat(term, fake, self.ws, self.home)

    def close(self) -> None:
        for term, fake in self.opened:
            term.close()
            fake.__exit__(None, None, None)


@pytest.fixture
def launch(tmp_path: Path) -> Iterator[Launcher]:
    launcher = Launcher(tmp_path)
    try:
        yield launcher
    finally:
        launcher.close()


# -- start and exit -------------------------------------------------------------------


def test_first_frame_alternate_screen_and_clean_exit(launch: Launcher) -> None:
    launch.git_init()
    chat = launch.start([], watch=("Type a task to start.", "approvals ask"))
    t = chat.term
    first = max(t.seen_at("Type a task to start."), t.seen_at("approvals ask"))
    assert first < 5.0  # the real figure is in docs/perf.md; this only guards a hang
    assert t.screen.alternate
    screen = t.lines()
    assert screen[0].startswith("╭─ KaroX ")
    assert "fake · openai-chat" in screen[1]
    assert screen[-1].strip().startswith("fake · ws (main)")
    assert screen[-1].rstrip().endswith("ctx ▱▱▱▱▱ 0% · approvals ask")
    assert typed(t) == ["›"]
    assert screen[-4].startswith("╭─")
    assert screen[-2].startswith("╰─")
    assert "enter send · alt+enter new line · / commands · @ files" in screen[-2]
    assert not screen[-5].strip()  # the activity line, blank between turns
    # The frame painted at start and the app's own first frame are the same picture.
    time.sleep(1.0)
    assert t.lines() == screen
    assert t.raw.count(ENTER_ALT) == 1  # entered once, not once per layer
    t.key("ctrl-d")
    assert t.wait_exit() == 0
    assert not t.screen.alternate
    assert (t.raw.count(ENTER_ALT), t.raw.count(LEAVE_ALT)) == (1, 1)
    # Back on the user's own screen: one line, how to come back; nothing of the chat left.
    assert "resume previous: karox6 chat --resume last" in t.text()
    assert "KaroX 6" not in t.text()
    assert "\x1b[?1000l" in t.raw  # mouse reporting is switched off again
    assert "\x1b[?2004l" in t.raw  # and bracketed paste
    # and the terminal handles typed keys itself again, as the shell expects
    assert t.input_mode() in (None, LINE_MODE)


def test_keys_typed_under_the_first_frame_reach_the_app_as_typed(launch: Launcher) -> None:
    """The first frame is on the screen before the app reads keys. A terminal in its line
    mode (POSIX) would handle what is typed in between itself: echo it over the frame and
    pass Enter on as Ctrl+J, a new line in the input box, so nothing would be sent."""
    chat = launch.start([Reply(text="pong")], ready=None, watch=("Type a task to start.",))
    t = chat.term
    t.seen_at("Type a task to start.")
    t.send("ping\r")  # typed at once, Enter included
    t.wait("pong")
    assert chat.last_user_message() == "ping"
    t.wait(lambda _s: chat.idle())
    t.key("ctrl-d")
    assert t.wait_exit() == 0
    assert t.input_mode() in (None, LINE_MODE)


def test_ctrl_c_under_the_first_frame_leaves_a_clean_terminal(launch: Launcher) -> None:
    """Ctrl+C while only the start-up frame is up (the app is still being imported) ends
    the start: the user's screen and the terminal's own input mode come back."""
    env = launch.interruptible()  # on Windows too: see term.ctrl_c_hook
    chat = launch.start([], ready=None, watch=("Type a task to start.",), env=env)
    t = chat.term
    t.seen_at("Type a task to start.")
    t.key("ctrl-c")
    # The terminal turns the key into an interrupt (the frame leaves that switched on) and
    # the start ends. If the app was already reading keys, the first Ctrl+C arms the exit.
    armed = "Press Ctrl+C again to exit."
    t.wait(lambda screen: not t.alive() or armed in screen, 15)
    if t.alive():
        t.key("ctrl-c")
    assert t.wait_exit() == 0
    assert not t.screen.alternate
    assert t.raw.count(ENTER_ALT) == t.raw.count(LEAVE_ALT) == 1
    assert t.input_mode() in (None, LINE_MODE)
    assert not [mark for mark in NOISE if mark in t.raw], t.raw


@pytest.mark.parametrize("how", ["plain", "exec", "callback"])
@pytest.mark.parametrize(
    "at",
    ["splash", "asyncio", "ssl", "karox6.tui_app"],
    ids=["before-the-frame", "under-the-frame", "inside-an-import", "loop-running"],
)
def test_an_interrupt_at_any_point_of_the_start_ends_it_cleanly(
    launch: Launcher, at: str, how: str
) -> None:
    """The race of the test above, without the race: the chat interrupts itself at an exact
    point of its start (as it first imports ``at``), in each of the ways that went wrong
    while Python's own handler raised KeyboardInterrupt there. Raised plainly before the
    event loop ran, it left the chat's coroutine unawaited and a warning on the user's
    screen, or a traceback when the frame was not painted yet; raised inside an `exec` of
    source text, the process returned 0 and was ended by the signal all the same; raised
    inside a callback, it was printed and dropped, so the key was lost and the chat stayed
    up. Now the start ends each time: exit 0, the user's screen and the terminal's input
    mode back, nothing printed."""
    chat = launch.start([], ready=None, env=launch.interruptible(f"{at}:{how}"))
    t = chat.term
    assert t.wait_exit(20) == 0
    assert not t.screen.alternate
    assert t.raw.count(ENTER_ALT) == t.raw.count(LEAVE_ALT) == 1
    assert t.input_mode() in (None, LINE_MODE)
    assert not [mark for mark in NOISE if mark in t.raw], t.raw


def test_ctrl_c_twice_leaves_and_names_the_session(launch: Launcher) -> None:
    chat = launch.start([Reply(text="pong")])
    chat.turn("ping")
    chat.term.key("ctrl-c")
    chat.term.wait("Press Ctrl+C again to exit.")
    chat.term.key("ctrl-c")
    assert chat.term.wait_exit() == 0
    out = chat.term.text()
    assert "(2 messages) — resume: karox6 chat --resume " in out.replace("\n", "")
    assert not chat.term.screen.alternate


def test_first_run_without_a_model_starts_with_setup(launch: Launcher) -> None:
    env = launch.env()
    del env["KAROX_API_KEY"]
    chat = launch.start([Reply(text="first answer")], model=False, ready="Number [1]:", env=env)
    t = chat.term
    assert "Set up KaroX: choose where the model runs." in t.text()
    t.line("6")
    t.wait("Server URL")
    t.line(chat.model.url)
    t.wait("Model:")
    t.line("fake")
    t.wait("Hidden input")
    t.key("enter")  # a local server needs no key
    t.wait("Type a task to start.")
    chat.turn("hi")
    assert "first answer" in t.text()
    config = (chat.home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "fake"' in config
    assert chat.model.url in config


def test_inline_mode_stays_in_the_scrollback(launch: Launcher) -> None:
    chat = launch.start(
        [Reply(text="inline answer"), Reply(calls=[PUSH]), Reply(text="ok")], args=("--inline",)
    )
    t = chat.term
    chat.say("hello", "inline answer")
    t.wait("✓ completed")
    t.line("push")
    t.wait("Run it? [y/N/a = always]")
    t.line("n")
    t.wait("not run (rule git.push-force)")
    t.wait(lambda s: s.count("✓ completed") == 2)
    t.key("ctrl-d")
    assert t.wait_exit() == 0
    assert t.screen.alt_entered == 0  # never left the normal screen
    assert "inline answer" in t.text()  # so the conversation is still there after exit


# -- commands -------------------------------------------------------------------------


def test_every_command_answers(launch: Launcher) -> None:
    launch.git_init()
    chat = launch.start([Reply(text="first answer"), Reply(text="second answer")])
    t = chat.term
    chat.say("/help", "Input & Shortcuts")
    t.key("pageup")  # the help is longer than this window: it scrolls like everything else
    t.wait("/undo")
    t.key("end")
    chat.say("/clear", "Type a task to start.")
    chat.say("/status", "messages   0")
    assert "context    0 / 400.0k (0%)" in t.text()  # the same number as the status bar
    chat.say("/cost", "requests   0")
    chat.say("/model", "model: fake")
    chat.say("/approve", "dangerous commands: ask")
    chat.say("/reasoning show", "reasoning shown")
    chat.say("/sessions", "no saved sessions")
    chat.say("/diff", "no changes")
    chat.say("/compact", "nothing to compact yet")
    chat.say("/undo", "nothing to undo")
    chat.say("/redo", "nothing to redo")
    chat.say("/fork", "nothing to fork yet")
    chat.say("/mcp", "MCP servers are switched off for this chat (--no-mcp)")
    chat.say("/stauts", "unknown command /stauts; did you mean /status? /help lists commands")
    chat.turn("hello")
    assert "first answer" in t.text()
    chat.say("/status", "messages   2")
    chat.say("/cost", "requests   1")
    chat.say("/sessions", "  hello")
    first = next(line for line in t.lines() if line.startswith("* ")).split()[1]
    chat.say("/fork", f"a copy of {first} (2 messages)")
    chat.say("/new", "new session ")
    chat.say("/resume", "resumed ")
    chat.say("/model other-model", "model: other-model")
    t.wait(lambda _s: status(t).strip().startswith("other-model"))
    chat.say("/approve allow", "dangerous commands: allow")
    t.wait(lambda _s: status(t).rstrip().endswith("approvals allow"))
    chat.turn("again")
    assert chat.model.requests[-1]["model"] == "other-model"
    t.line("/quit")
    assert t.wait_exit() == 0


def test_key_is_hidden_and_setup_switches_the_model(launch: Launcher) -> None:
    chat = launch.start([Reply(text="answer from the new model")])
    t = chat.term
    t.line("/key")
    t.wait("Hidden input (not echoed)")
    t.send("sk-secret-value-12345")
    t.wait(lambda s: "*" * 21 in s)
    assert "sk-secret" not in t.text()
    t.key("enter")
    t.wait("key saved (encrypted for your user)")
    assert "sk-secret-value-12345" not in t.raw  # never written to the terminal at all
    assert b"sk-secret" not in (chat.home / "credentials").read_bytes()
    t.line("/key")
    t.wait("Hidden input")
    t.send("abc")
    t.key("esc")
    t.wait("key unchanged")
    t.line("/setup")
    t.wait("Number [1]:")
    t.key("esc")
    t.wait("setup cancelled")
    t.line("/setup")
    t.wait("Number [1]:")
    t.line("6")
    t.wait("Server URL")
    t.line(chat.model.url)
    t.wait("Model:")
    t.line("fake-two")
    t.wait("Hidden input")
    t.key("enter")
    t.wait("now using fake-two")
    chat.turn("hi")
    assert chat.model.requests[-1]["model"] == "fake-two"
    assert chat.model.requests[-1]["messages"][-1]["content"] == "hi"


# -- turns: tools, changes, undo ------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_edit_shows_its_change_and_undo_takes_it_back(launch: Launcher) -> None:
    launch.git_init()
    edit = {
        "file_path": "hello.py",
        "old_string": "print('hi')",
        "new_string": "print('hello')\nprint('bye')",
    }
    replies = [
        Reply(reasoning="Look first.", calls=[("read", {"file_path": "hello.py"})]),
        Reply(calls=[("edit", edit)]),
        Reply(text="Changed the greeting."),
    ]
    chat = launch.start(replies)
    t = chat.term
    screen = chat.turn("change the greeting")
    assert "● read hello.py  ✓ 1 lines" in screen
    assert "● edit hello.py  ✓ +2 -1" in screen
    assert "      1 - print('hi')" in screen  # the change itself, numbered, under the call
    assert "      1 + print('hello')" in screen
    assert "      2 + print('bye')" in screen
    assert "1 file changed (/diff)" in screen
    chat.say("/diff", "+print('bye')")
    assert "1 file changed  +2 -1" in t.text()
    chat.say("/undo", "undid: change the greeting")
    t.wait("restored 1 file: hello.py")
    assert (chat.ws / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    t.wait(lambda _s: typed(t)[-1:] == ["› change the greeting"])  # back in the input line
    t.key("ctrl-c")  # clear it
    chat.say("/redo", "re-applied 1 file: hello.py")
    assert (chat.ws / "hello.py").read_text(encoding="utf-8") == "print('hello')\nprint('bye')\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=chat.ws, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, check=True,
    ).stdout  # fmt: skip
    assert status.split() == ["M", "hello.py"]  # the user's repository saw nothing else


def test_approvals_yes_no_always_and_escape(launch: Launcher) -> None:
    replies = [
        Reply(calls=[PUSH]),
        Reply(text="tried"),
        Reply(calls=[PUSH]),
        Reply(text="refused, fine"),
        Reply(calls=[PUSH]),  # Esc at the question stops the turn
        Reply(calls=[PUSH]),
        Reply(calls=[PUSH]),  # after "always": no question
        Reply(text="done with always"),
    ]
    chat = launch.start(replies)
    t = chat.term
    t.line("push it")
    t.wait("Run it? [y/N/a = always]")
    assert "! Approval required: y run · n deny · a always · esc stop turn" in t.text()
    assert "rule: git.push-force" in t.text()
    t.line("y")
    t.wait("tried")
    assert "✗ exit " in t.text()  # it really ran (and failed: there is no remote)
    t.line("push again")
    t.wait("Approval required")  # shown only while a question is open
    t.line("n")
    t.wait("refused, fine")
    assert "✗ not run (rule git.push-force)" in t.text()
    t.line("push third")
    t.wait("Approval required")
    t.key("esc")
    t.wait("! stopped")
    t.wait_gone("Approval required")
    t.line("push fourth")
    t.wait("Approval required")
    t.line("a")
    t.wait("done with always")  # the second push of that turn asked nothing
    assert "Approval required" not in t.text()
    assert status(t).rstrip().endswith("approvals allow")


def test_escape_and_ctrl_c_stop_only_the_turn(launch: Launcher) -> None:
    replies = [
        Reply(text="alpha " * 300, chunk_size=12, chunk_delay=0.05),
        Reply(text="beta " * 300, chunk_size=10, chunk_delay=0.05),
        Reply(text="still here"),
    ]
    chat = launch.start(replies)
    t = chat.term
    for stops, (word, key) in enumerate((("alpha", "esc"), ("beta", "ctrl-c")), 1):
        t.line("go slowly")
        t.wait(f"{word} {word} {word}")  # the reply is streaming
        t.key(key)
        t.wait(lambda s, n=stops: s.count("! stopped") == n, 15)
        assert t.alive()
    chat.turn("are you there?")
    assert "still here" in t.text()
    # the stopped turns are in the history as the user's messages, without half a reply
    roles = [m["role"] for m in chat.model.requests[-1]["messages"]]
    assert roles.count("user") == 3


def test_a_message_typed_while_the_agent_works_waits_its_turn(launch: Launcher) -> None:
    replies = [
        Reply(text="slow " * 60, chunk_size=10, chunk_delay=0.05),
        Reply(text="answer to the queued one"),
    ]
    chat = launch.start(replies)
    t = chat.term
    t.line("first")
    t.wait("slow slow")
    t.send("queued while running")
    t.wait(lambda _s: typed(t)[-1:] == ["› queued while running"])  # typing is not blocked
    t.key("enter")
    t.wait("1 queued (esc clears)")  # and the message is not silently swallowed
    t.wait("answer to the queued one", 40)
    assert chat.last_user_message() == "queued while running"
    assert "queued" not in activity(t)


def test_alt_enter_while_the_agent_works_is_a_newline_not_a_stop(launch: Launcher) -> None:
    replies = [
        Reply(text="slow " * 60, chunk_size=10, chunk_delay=0.05),
        Reply(text="got both lines"),
    ]
    chat = launch.start(replies)
    t = chat.term
    t.line("first")
    t.wait("slow slow")
    t.send("top")
    time.sleep(0.3)
    t.send("\x1b\r")  # Alt+Enter: Escape and Enter in one read
    time.sleep(0.3)
    t.send("bottom")
    time.sleep(0.4)
    t.key("enter")
    t.wait("got both lines", 40)
    assert "! stopped" not in t.text()
    assert chat.last_user_message() == "top\nbottom"


# -- input ----------------------------------------------------------------------------


def test_completion_multiline_history_and_search(launch: Launcher) -> None:
    chat = launch.start([Reply(text=f"answer {n}") for n in range(1, 6)])
    t = chat.term
    size = (chat.ws / "hello.py").stat().st_size  # 13 bytes where text files end lines in CRLF
    t.send("look at @he")
    t.wait(f"@hello.py  {size} B")
    t.key("tab")
    t.wait(lambda _s: typed(t)[-1:] == ["› look at @hello.py"])
    t.key("enter")  # takes the completion, does not send
    assert len(chat.model.requests) == 0
    t.key("enter")
    t.wait(f"attached: hello.py ({size} B)")
    t.wait("answer 1")
    assert '<attached_file path="hello.py">' in str(chat.last_user_message())
    t.send("@зам")
    t.wait("@docs/заметки.md")
    t.key("tab", "enter", "enter")
    t.wait("answer 2")
    assert "# Заметки" in str(chat.last_user_message())
    t.send("/")
    t.wait("start a fresh session")
    t.send("un")
    t.wait("take back the last turn")
    t.key("esc", "backspace", "backspace", "backspace")
    t.wait(lambda _s: typed(t)[-1:] == ["›"])
    # three ways to break a line, one message
    t.send("line one")
    t.key("alt-enter")
    t.send("line two")
    t.key("ctrl-j")
    t.send("line three\\")
    t.key("enter")
    t.send("line four")
    t.wait(lambda _s: typed(t) == ["› line one", "… line two", "… line three", "… line four"])
    t.key("enter")
    t.wait("answer 3")
    assert chat.last_user_message() == "line one\nline two\nline three\nline four"
    t.key("up")
    t.wait(lambda _s: typed(t)[-1:] == ["… line four"])  # history brings the whole message back
    t.key("ctrl-c")
    t.key("ctrl-r")
    t.send("look")
    t.wait("I-search backward: look")
    t.key("enter")
    t.wait(lambda _s: typed(t)[-1:] == ["› look at @hello.py"])
    t.key("ctrl-c")


def test_pasted_lines_are_one_message(launch: Launcher) -> None:
    chat = launch.start([Reply(text="got the paste"), Reply(text="got the second")])
    t = chat.term
    t.paste("bracketed one\nbracketed two\nbracketed three")
    t.wait(lambda _s: typed(t)[-1:] == ["… bracketed three"])
    assert len(chat.model.requests) == 0  # a paste never sends by itself
    t.key("enter")
    t.wait("got the paste")
    assert chat.last_user_message() == "bracketed one\nbracketed two\nbracketed three"
    # A terminal without bracketed paste sends the same text as plain keys.
    t.send("plain one\rplain two\rplain three")
    t.wait(lambda _s: typed(t)[-1:] == ["… plain three"])
    assert len(chat.model.requests) == 1  # was: every line sent as a message of its own
    t.key("enter")
    t.wait("got the second")
    assert chat.last_user_message() == "plain one\nplain two\nplain three"


def test_a_picture_named_in_the_message_reaches_the_model(launch: Launcher) -> None:
    sys.path.insert(0, str(SRC))
    from karox6.tools.screen import encode_png

    (launch.ws / "shot.png").write_bytes(encode_png(4, 3, bytes([10, 200, 30]) * 12))
    chat = launch.start([Reply(text="a green rectangle")])
    chat.turn("what is on @shot.png ?")
    assert "[picture 1: 4x3 PNG, " in chat.term.text()
    content = chat.last_user_message()
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is on [image 1] ?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")


def test_a_long_paste_is_a_placeholder_and_one_message(launch: Launcher) -> None:
    chat = launch.start([Reply(text="read the paste")])
    t = chat.term
    data = "\n".join(f"line {i} of a long paste" for i in range(1, 5001))
    t.send("\x1b[200~")
    for i in range(0, len(data), 8192):  # a real paste arrives in chunks
        t.send(data[i : i + 8192])
    t.send("\x1b[201~")
    t.wait(lambda _s: any("[pasted 5,000 lines · " in line for line in typed(t)), 30)
    assert "line 1 of a long paste" not in t.text()  # the payload stays out of the editor
    assert len(chat.model.requests) == 0  # a paste never sends by itself
    t.key("enter")
    t.wait("read the paste")
    assert chat.last_user_message() == data


def test_ctrl_v_pastes_clipboard_text_and_attaches_pictures(launch: Launcher) -> None:
    sys.path.insert(0, str(SRC))
    from karox6.tools.screen import encode_png

    clip = launch.tmp / "clip"
    clip.mkdir()
    (clip / "text").write_text("text off the clipboard", encoding="utf-8")
    chat = launch.start(
        [Reply(text="got the text"), Reply(text="two pictures")],
        env=launch.env(KAROX_FAKE_CLIPBOARD=str(clip)),
    )
    t = chat.term
    t.send("\x16")  # Ctrl+V
    t.wait(lambda _s: typed(t)[-1:] == ["› text off the clipboard"])
    t.key("enter")
    t.wait("got the text")
    assert chat.last_user_message() == "text off the clipboard"
    # A clipboard with a picture but no text attaches it instead. Windows Terminal
    # sends nothing at all for Ctrl+V then, so an empty bracketed paste is how the
    # ask arrives; Ctrl+V itself asks the same way. One of each here.
    (clip / "text").unlink()
    (clip / "image.png").write_bytes(encode_png(4, 3, bytes([10, 200, 30]) * 12))
    t.send("\x1b[200~\x1b[201~")
    t.wait("[picture 1: 4x3 PNG, ")
    t.send("\x16")
    t.wait("[picture 2: 4x3 PNG, ")
    chat.turn("two pictures")
    content = chat.last_user_message()
    assert isinstance(content, list)
    assert len(content) == 3
    assert content[0] == {"type": "text", "text": "two pictures"}
    assert all(
        part["image_url"]["url"].startswith("data:image/png;base64,iVBOR") for part in content[1:]
    )


def test_drag_selecting_the_answer_copies_plain_text(launch: Launcher) -> None:
    clip = launch.tmp / "clip"
    clip.mkdir()
    chat = launch.start(
        [Reply(text="the quick brown fox jumped")],
        env=launch.env(KAROX_FAKE_CLIPBOARD=str(clip)),
    )
    t = chat.term
    chat.turn("a sentence please")
    row, col = next((i, line.index("quick")) for i, line in enumerate(t.lines()) if "quick" in line)
    y, x = row + 1, col + 1  # the terminal counts rows and columns from 1
    t.send(f"\x1b[<0;{x};{y}M")  # left button down
    t.send(f"\x1b[<32;{x + 11};{y}M")  # drag 11 cells right
    t.send(f"\x1b[<0;{x + 11};{y}m")  # release: the selection is copied
    t.wait("copied ", 15)
    copied = (clip / "text").read_text(encoding="utf-8")
    assert "quick brown" in copied
    assert all(ch not in copied for ch in "│╭╮╰╯─")  # plain text, no frame pieces


def test_copy_put_answers_and_code_on_the_clipboard(launch: Launcher) -> None:
    clip = launch.tmp / "clip"
    clip.mkdir()
    chat = launch.start(
        [Reply(text="here is the code\n\n```python\nsome_code()\n```\n"), Reply(text="2nd")],
        env=launch.env(KAROX_FAKE_CLIPBOARD=str(clip)),
    )
    t = chat.term
    t.line("/copy")
    t.wait("nothing to copy yet")
    chat.turn("give me code")
    t.line("/copy")
    t.wait("copied ")
    assert (clip / "text").read_text(encoding="utf-8") == (
        "here is the code\n\n```python\nsome_code()\n```"
    )
    t.line("/copy code")
    t.wait(lambda _s: (clip / "text").read_text(encoding="utf-8") == "some_code()", 15)
    chat.turn("another")
    t.line("/copy 2")
    t.wait(lambda _s: (clip / "text").read_text(encoding="utf-8").startswith("here is"), 15)
    t.line("/copy bogus")
    t.wait("usage: /copy, /copy code or /copy N")


# -- long sessions, resizing, errors --------------------------------------------------


def test_long_output_scrolls_and_survives_resizing(launch: Launcher) -> None:
    text = "\n".join(f"строка {i}: " + "слово " * (i % 9 + 1) for i in range(1, 121))
    wide = "wide: 日本語のテキスト and emoji 🙂 and a tab\there " + "x" * 150
    chat = launch.start([Reply(text=text, chunk_size=200), Reply(text=wide)], rows=24, cols=80)
    t = chat.term
    chat.turn("long output please")
    assert t.lines()[-7].startswith("строка 120: слово")
    t.key("pageup")
    t.wait("↑ 17 lines below (end)")  # a page: 19 rows less 2
    assert "строка 100:" in t.text()
    t.key("ctrl-home")
    t.wait("╭─ KaroX")
    assert t.lines()[0].startswith("╭─ KaroX")
    assert "› long output please" in t.text()
    t.key("end")
    t.wait_gone("lines below")
    assert t.lines()[-7].startswith("строка 120: слово")
    for _ in range(5):  # the mouse wheel, as terminals report it
        t.send("\x1b[<64;10;10M")
    t.wait("↑ 15 lines below (end)")
    t.key("ctrl-end")
    t.wait_gone("lines below")
    for rows, cols in ((30, 120), (14, 46), (24, 80)):
        t.resize(rows, cols)
        t.wait(lambda _s, c=cols: status(t).rstrip().endswith("ask") and len(status(t)) <= c)
        assert typed(t) == ["›"]
        assert any(line.startswith("строка 120:") for line in t.lines())
        assert status(t).strip().startswith("fake")  # narrow: model and approvals stay
    chat.turn("wide characters")
    screen = t.text()
    # 45 cells before the tab (wide characters count twice): the next stop is cell 48
    assert "日本語のテキスト and emoji 🙂 and a tab   here" in screen
    assert all(len(line) <= 80 for line in t.lines())


def test_provider_errors_are_sentences_with_a_next_step(launch: Launcher) -> None:
    replies = [
        Reply(status=401, body='{"error": {"message": "Incorrect API key provided: sk-***"}}'),
        Reply(
            status=429,
            body='{"error": {"message": "Rate limit reached"}}',
            headers={"retry-after": "1"},
        ),
        Reply(text="after the wait"),
        Reply(text="half an answer that the network cuts " * 4, cut_stream=True),
        Reply(text="whole answer"),
        Reply(status=404, body='{"error": {"message": "The model `fake` does not exist"}}'),
    ]
    chat = launch.start(replies)
    t = chat.term
    t.line("bad key")
    t.wait("✗ error: the API key was refused (HTTP 401): Incorrect API key provided: sk-***")
    t.wait("/key sets a new key; /setup changes the provider")
    t.wait("! error")
    assert '{"error"' not in t.text()  # no raw JSON
    assert t.text().count("Incorrect API key") == 1  # and not printed twice
    t.line("too fast")
    t.wait("retry 1 in 1s: rate limit (HTTP 429): Rate limit reached")
    t.wait("after the wait")
    t.line("flaky network")
    t.wait("retry 1 in")
    t.wait("whole answer")
    assert "cannot reach the model server: stream ended before the response finished" in t.text()
    t.line("wrong model")
    t.wait("the model name was not accepted (HTTP 404)")
    t.wait("/model NAME sets another model")
    assert t.alive()


# -- the look: input box, activity line, markdown, folded tool output, colours ---------


def _cell(term: Terminal, text: str) -> Any:
    """The screen cell where ``text`` first starts (pyte's character with its colours)."""
    for y, line in enumerate(term.lines()):
        x = line.find(text)
        if x >= 0:
            return term.screen.buffer[y][x]
    raise AssertionError(f"{text!r} is not on the screen\n{term.dump()}")


def test_activity_line_and_keys_follow_the_turn(launch: Launcher) -> None:
    replies = [Reply(text="slow " * 80, chunk_size=8, chunk_delay=0.05), Reply(text="done")]
    chat = launch.start(replies, env=launch.env(COLORTERM="truecolor"))
    t = chat.term
    t.line("work")
    t.wait(lambda _s: "esc stop" in activity(t))
    line = activity(t)
    assert re.search(r"^ \S \S.* +\d+\.\ds · esc stop$", line.rstrip())  # spinner, phase
    assert (
        t.lines()[-2].rstrip().endswith("esc stop · enter queue a message · ctrl+o tool output ─╯")
    )
    t.wait(lambda _s: chat.idle(), 40)
    assert not activity(t).strip()
    assert "enter send" in t.lines()[-2]
    assert re.search(r" \d+\.\ds · approvals ask$", status(t).rstrip())  # the turn's time


def test_markdown_answers_are_formatted_on_screen(launch: Launcher) -> None:
    answer = (
        "## Result\n\nThe fix is **small**:\n\n- one `call`\n- two\n\n"
        "```python\ndef f():\n    return 1\n```\n\n| a | b |\n|---|---|\n| x | yy |\n"
    )
    chat = launch.start([Reply(text=answer, chunk_size=5)], env=launch.env(COLORTERM="truecolor"))
    t = chat.term
    screen = chat.turn("explain")
    for line in ("## Result", "The fix is small:", "• one call", "• two", "```python"):
        assert line in screen
    assert "a │ b" in screen
    assert "──┼───" in screen
    assert "x │ yy" in screen
    assert _cell(t, "Result").bold
    assert _cell(t, "small").bold
    assert _cell(t, "call").fg not in ("default", _cell(t, "one").fg)  # inline code colour
    assert _cell(t, "def").fg != "default"  # code is coloured


def test_tool_output_is_folded_and_ctrl_o_unfolds_it(launch: Launcher) -> None:
    python = sys.executable.replace("\\", "/")  # the same in bash and PowerShell
    command = f"{python} -c \"for i in range(6): print('row %d' % i)\""
    replies = [Reply(calls=[("bash", {"command": command})]), Reply(text="ran it")]
    chat = launch.start(replies)
    t = chat.term
    chat.turn("run")
    t.wait("… 6 lines (ctrl+o to expand)")
    assert "row 2" not in t.text()
    # The long command wraps; a wrap that falls on a space drops it from the joined text.
    joined = "".join(t.lines())
    assert re.search(r"● bash .*python.* ? ?✓ ?exit 0 · row 5 · [0-9.]+s", joined)
    t.send("\x0f")  # Ctrl+O
    t.wait("└ row 0")
    assert all(f"    row {i}" in t.text() for i in range(1, 6))
    assert "ctrl+o to expand" not in t.text()
    t.send("\x0f")
    t.wait("… 6 lines (ctrl+o to expand)")
    assert "row 2" not in t.text()


def test_unfolded_tool_output_keeps_its_indentation(launch: Launcher) -> None:
    # Code and diffs printed by a command must keep their leading and inner spaces.
    python = sys.executable.replace("\\", "/")
    command = f"{python} -c \"print('top'); print('    four'); print('-    minus'); print('end')\""
    chat = launch.start([Reply(calls=[("bash", {"command": command})]), Reply(text="ran it")])
    t = chat.term
    chat.turn("run")
    t.wait("… 4 lines (ctrl+o to expand)")
    t.send("\x0f")  # Ctrl+O
    t.wait("└ top")
    screen = t.text()
    assert "        four" in screen  # 4 of the block + 4 of the output
    assert "    -    minus" in screen


def test_update_notice_in_the_conversation_and_the_status_bar(launch: Launcher) -> None:
    import json

    launch.home.mkdir(exist_ok=True)
    state = {
        "repo": "https://github.com/kar0777/KaroX.git",
        "branch": "karox6/main",
        "checked": time.time(),  # checked a moment ago: no network now
        "version": "99.0",
        "commit": "f" * 40,
    }
    (launch.home / "update-check.json").write_text(json.dumps(state), encoding="utf-8")
    chat = launch.start([], env=launch.env(KAROX_UPDATE_CHECK="1"))
    t = chat.term
    t.wait("version 99.0 is available: /update")
    t.wait(lambda _s: "↑ 99.0 /update" in status(t))


def test_without_the_check_nothing_is_said(launch: Launcher) -> None:
    import json

    launch.home.mkdir(exist_ok=True)
    state = {"repo": "https://github.com/kar0777/KaroX.git", "branch": "karox6/main"}
    state |= {"checked": time.time(), "version": "99.0", "commit": "f" * 40}
    (launch.home / "update-check.json").write_text(json.dumps(state), encoding="utf-8")
    chat = launch.start([])  # a source tree: the check stays off
    time.sleep(1.0)
    assert "99.0" not in chat.term.text()


@pytest.mark.parametrize(
    ("extra", "wanted", "unwanted"),
    [
        ({"COLORTERM": "truecolor"}, "\x1b[38;2;", ""),
        ({"KAROX_COLOR_DEPTH": "8"}, "38;5;", "38;2;"),
        ({"KAROX_COLOR_DEPTH": "4"}, "\x1b[", "38;[25];"),
        ({"NO_COLOR": "1"}, "╭─ KaroX", r"\x1b\[(?:[0-9;]*;)?(?:3[0-7]|38;[25])[;m]"),
        ({"COLORTERM": "truecolor", "KAROX_THEME": "light"}, "38;2;37;99;201", ""),
    ],
    ids=["truecolor", "256", "16", "no-color", "light"],
)
def test_colour_depths_and_themes(
    launch: Launcher, extra: dict[str, str], wanted: str, unwanted: str
) -> None:
    chat = launch.start([Reply(text="**bold** and `code`")], env=launch.env(**extra))
    t = chat.term
    chat.turn("hi")
    assert wanted in t.raw
    if unwanted:
        assert re.search(unwanted, t.raw) is None
    # without colours the answer's markdown stays as the model wrote it
    shown = "**bold** and `code`" if "NO_COLOR" in extra else "bold and code"
    assert shown in t.text()
    assert typed(t) == ["›"]


@pytest.mark.skipif(sys.platform != "win32", reason="the classic Windows console")
def test_classic_console_gets_symbols_its_fonts_have(launch: Launcher) -> None:
    env = {k: v for k, v in launch.env().items() if k not in ("TERM", "WT_SESSION")}
    replies = [Reply(calls=[("read", {"file_path": "hello.py"})]), Reply(text="read")]
    chat = launch.start(replies, env=env)
    screen = chat.turn("read it")
    assert "● read hello.py  √ 1 lines" in screen
    assert "✓" not in screen
