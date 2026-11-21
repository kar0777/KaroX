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

import pytest

pytest.importorskip("pyte")
if sys.platform == "win32":
    pytest.importorskip("winpty")

from fake_model import FakeModel, Reply
from term import Terminal

SRC = Path(__file__).resolve().parents[1] / "src"
ENTER_ALT, LEAVE_ALT = "\x1b[?1049h", "\x1b[?1049l"
PUSH = ("bash", {"command": "git push --force origin main"})  # needs approval (git.push-force)
_ACTIVITY = re.compile(r"\d+\.\ds  \S")


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
        """No turn is running: the status bar shows no "12.3s  <phase>" activity."""
        return not any(_ACTIVITY.search(line) for line in self.term.lines()[-3:])

    def turn(self, text: str, timeout: float = 30.0) -> str:
        """Send a message and wait until its turn has ended (the model was asked and the
        status bar is quiet again)."""
        asked = len(self.model.requests)
        self.term.line(text)
        self.term.wait(lambda _s: len(self.model.requests) > asked and self.idle(), timeout)
        return self.term.text()

    def last_user_message(self) -> object:
        return self.model.requests[-1]["messages"][-1]["content"]


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
    chat = launch.start([], watch=("Type a task to start.", "approve:ask"))
    t = chat.term
    first = max(t.seen_at("Type a task to start."), t.seen_at("approve:ask"))
    assert first < 5.0  # the real figure is in docs/perf.md; this only guards a hang
    assert t.screen.alternate
    screen = t.lines()
    assert screen[0].startswith("╭─ KaroX ")
    assert "fake · openai-chat" in screen[1]
    assert screen[-2].strip().startswith("fake · ws (main)")
    assert screen[-2].rstrip().endswith("ctx 0/400.0k (0%) · approve:ask")
    assert screen[-1].strip() == "›"
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
    t.wait("[completed]")
    t.line("push")
    t.wait("Run it? [y/N/a = always]")
    t.line("n")
    t.wait("not run (rule git.push-force)")
    t.wait(lambda s: s.count("[completed]") == 2)
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
    t.wait(lambda _s: t.lines()[-2].strip().startswith("other-model"))
    chat.say("/approve allow", "dangerous commands: allow")
    t.wait(lambda _s: t.lines()[-2].rstrip().endswith("approve:allow"))
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
    assert "1 files changed (/diff)" in screen
    chat.say("/diff", "+print('bye')")
    assert "1 file changed  +2 -1" in t.text()
    chat.say("/undo", "undid: change the greeting")
    t.wait("restored 1 file: hello.py")
    assert (chat.ws / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    t.wait(lambda _s: t.lines()[-1] == "› change the greeting")  # back in the input line
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
    assert "Approval required — y: run · n: deny · a: always · Esc: stop turn" in t.text()
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
    t.wait("[stopped]")
    t.wait_gone("Approval required")
    t.line("push fourth")
    t.wait("Approval required")
    t.line("a")
    t.wait("done with always")  # the second push of that turn asked nothing
    assert "Approval required" not in t.text()
    assert t.lines()[-2].rstrip().endswith("approve:allow")


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
        t.wait(lambda s, n=stops: s.count("[stopped]") == n, 15)
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
    t.wait(lambda _s: t.lines()[-1] == "› queued while running")  # typing is not blocked
    t.key("enter")
    t.wait("1 queued (Esc clears)")  # and the message is not silently swallowed
    t.wait("answer to the queued one", 40)
    assert chat.last_user_message() == "queued while running"
    assert "queued (Esc" not in t.lines()[-2]


# -- input ----------------------------------------------------------------------------


def test_completion_multiline_history_and_search(launch: Launcher) -> None:
    chat = launch.start([Reply(text=f"answer {n}") for n in range(1, 6)])
    t = chat.term
    t.send("look at @he")
    t.wait("@hello.py  13 B")
    t.key("tab")
    t.wait(lambda _s: t.lines()[-1] == "› look at @hello.py")
    t.key("enter")  # takes the completion, does not send
    assert len(chat.model.requests) == 0
    t.key("enter")
    t.wait("attached: hello.py (13 B)")
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
    t.wait(lambda _s: t.lines()[-1] == "›")
    # three ways to break a line, one message
    t.send("line one")
    t.key("alt-enter")
    t.send("line two")
    t.key("ctrl-j")
    t.send("line three\\")
    t.key("enter")
    t.send("line four")
    t.wait(lambda _s: t.lines()[-4:] == ["› line one", "… line two", "… line three", "… line four"])
    t.key("enter")
    t.wait("answer 3")
    assert chat.last_user_message() == "line one\nline two\nline three\nline four"
    t.key("up")
    t.wait(lambda _s: t.lines()[-1] == "… line four")  # history brings the whole message back
    t.key("ctrl-c")
    t.key("ctrl-r")
    t.send("look")
    t.wait("I-search backward: look")
    t.key("enter")
    t.wait(lambda _s: t.lines()[-1] == "› look at @hello.py")
    t.key("ctrl-c")


def test_pasted_lines_are_one_message(launch: Launcher) -> None:
    chat = launch.start([Reply(text="got the paste"), Reply(text="got the second")])
    t = chat.term
    t.paste("bracketed one\nbracketed two\nbracketed three")
    t.wait(lambda _s: t.lines()[-1] == "… bracketed three")
    assert len(chat.model.requests) == 0  # a paste never sends by itself
    t.key("enter")
    t.wait("got the paste")
    assert chat.last_user_message() == "bracketed one\nbracketed two\nbracketed three"
    # A terminal without bracketed paste sends the same text as plain keys.
    t.send("plain one\rplain two\rplain three")
    t.wait(lambda _s: t.lines()[-1] == "… plain three")
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
    assert "pictures: shot.png (4x3, " in chat.term.text()
    content = chat.last_user_message()
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is on [image 1] ?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")


# -- long sessions, resizing, errors --------------------------------------------------


def test_long_output_scrolls_and_survives_resizing(launch: Launcher) -> None:
    text = "\n".join(f"строка {i}: " + "слово " * (i % 9 + 1) for i in range(1, 121))
    wide = "wide: 日本語のテキスト and emoji 🙂 and a tab\there " + "x" * 150
    chat = launch.start([Reply(text=text, chunk_size=200), Reply(text=wide)], rows=24, cols=80)
    t = chat.term
    chat.turn("long output please")
    assert t.lines()[-4].startswith("строка 120: слово")
    t.key("pageup")
    t.wait("scrolled +20L (End)")
    assert "строка 100:" in t.text()
    t.key("ctrl-home")
    t.wait("╭─ KaroX")
    assert t.lines()[0].startswith("╭─ KaroX")
    assert "› long output please" in t.text()
    t.key("end")
    t.wait_gone("scrolled +")
    assert t.lines()[-4].startswith("строка 120: слово")
    for _ in range(5):  # the mouse wheel, as terminals report it
        t.send("\x1b[<64;10;10M")
    t.wait("scrolled +15L (End)")
    t.key("ctrl-end")
    t.wait_gone("scrolled +")
    for rows, cols in ((30, 120), (14, 46), (24, 80)):
        t.resize(rows, cols)
        t.wait(
            lambda _s, c=cols: (
                t.lines()[-2].rstrip().endswith("approve:ask") and len(t.lines()[-2]) <= c
            )
        )
        assert t.lines()[-1].strip() == "›"
        assert any(line.startswith("строка 120:") for line in t.lines())
        assert t.lines()[-2].strip().startswith("fake")  # narrow: model and approvals stay
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
    t.wait("[error]")
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
