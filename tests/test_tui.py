import asyncio
import os
import subprocess
import sys
import threading

import pytest

from karox6 import cli
from karox6.core import events as ev
from karox6.core.session import CANCELLED_RESULT, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ApprovalRequest, ToolResult
from karox6.tui import ChatApp, Palette, _glyphs, brief
from karox6.tui_text import short_path


class FakeTerm:
    def __init__(self, lines=(), answers=()):
        self.lines = list(lines)
        self.answers = list(answers)
        self.prompts = []
        self.out = []

    async def read_line(self, prompt):
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    async def read_answer(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def write(self, text):
        self.out.append(text)

    @property
    def text(self):
        return "".join(self.out)


class ApprovalTool:
    name = "danger"
    description = "needs approval"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    async def run(self, args, ctx):
        ok = await ctx.approve(
            ApprovalRequest("danger", "rm -rf build", "rm.test", "deletes files")
        )
        return ToolResult("ran" if ok else "denied by user", is_error=not ok)


class HangTool:
    name = "hang"
    description = "never finishes"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self):
        self.started = asyncio.Event()

    async def run(self, args, ctx):
        self.started.set()
        await asyncio.sleep(3600)
        return ToolResult("unreachable")


def make_app(tmp_path, turns, term, **kwargs):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    config = SessionConfig(model="test", workspace=ws, home=tmp_path / "home")
    provider = ScriptedProvider(list(turns))
    app = ChatApp(
        config=config,
        provider=provider,
        read_line=term.read_line,
        read_answer=term.read_answer,
        write=term.write,
        **kwargs,
    )
    return app, provider


def tool_results(session):
    return [m.content for m in session.messages if m.role == "tool"]


def test_turns_share_history(tmp_path):
    term = FakeTerm(["hello", "and again", "/quit"])
    app, provider = make_app(tmp_path, [Turn(text="first answer"), Turn(text="second")], term)
    assert asyncio.run(app.run()) == 0
    assert "first answer" in term.text
    assert "second" in term.text
    assert term.text.count("✓ completed") == 2
    seen = [m.content for m in provider.requests[1].messages]
    assert seen == ["hello", "first answer", "and again"]


def test_welcome_card_shows_model_project_branch_and_keys(tmp_path):
    term = FakeTerm(["/quit"])
    app, _ = make_app(tmp_path, [], term)
    git = tmp_path / "ws" / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/feature-x\n", encoding="utf-8")
    asyncio.run(app.run())
    card = term.text.split("Type a task to start.")[0]
    for part in ("KaroX ", "test", "openai-chat", short_path(str(tmp_path / "ws")), "feature-x"):
        assert part in card
    for key in ("/ commands", "@ attach a file", "Alt+Enter", "Esc"):
        assert key in card
    lines = [ln for ln in card.splitlines() if ln.strip()]
    assert len(lines) >= 8
    assert len({len(ln) for ln in lines}) == 1  # the frame is closed and aligned


def test_multiline_input_and_eof(tmp_path):
    term = FakeTerm(["line one\\", "line two"])  # then EOF ends the chat
    app, _ = make_app(tmp_path, [Turn(text="ok")], term)
    asyncio.run(app.run())
    assert app.session.messages[0].content == "line one\nline two"
    assert term.prompts[:2] == ["› ", "… "]


def test_commands(tmp_path):
    lines = ["/help", "/status", "/approve deny", "/reasoning", "/bogus", "/new", "/exit"]
    term = FakeTerm(lines)
    app, _ = make_app(tmp_path, [], term)
    first = app.session.session_id
    asyncio.run(app.run())
    text = term.text
    assert "/diff" in text  # help
    assert f"session    {first}" in text
    assert "dangerous commands: deny" in text
    assert app.approval_mode == "deny"
    assert app.show_reasoning is True
    assert "unknown command /bogus" in text
    assert app.session.session_id != first
    assert f"new session {app.session.session_id}" in text


def test_approvals_ask_then_always(tmp_path):
    turns = [Turn(calls=[("danger", {})]), Turn(text="done")] * 4
    term = FakeTerm(["one", "two", "three", "four", "/quit"], answers=["n", "y", "a"])
    app, _ = make_app(tmp_path, turns, term, extra_tools=[ApprovalTool()])
    asyncio.run(app.run())
    assert tool_results(app.session) == ["denied by user", "ran", "ran", "ran"]
    assert term.answers == []  # the fourth call ran without asking
    assert app.approval_mode == "allow"
    assert "deletes files" in term.text


def test_deny_mode_refuses_without_asking(tmp_path):
    term = FakeTerm(["go", "/quit"])
    turns = [Turn(calls=[("danger", {})]), Turn(text="done")]
    app, _ = make_app(tmp_path, turns, term, extra_tools=[ApprovalTool()], approval_mode="deny")
    asyncio.run(app.run())
    assert tool_results(app.session) == ["denied by user"]
    assert "refused: rm -rf build" in term.text


def test_cancel_stops_only_the_turn(tmp_path):
    term = FakeTerm()
    turns = [Turn(calls=[("hang", {})]), Turn(text="after")]
    tool = HangTool()
    app, provider = make_app(tmp_path, turns, term, extra_tools=[tool])

    async def go():
        running = asyncio.create_task(app.turn("start"))
        await asyncio.wait_for(tool.started.wait(), 2)
        running.cancel()  # what Ctrl+C does to the main task
        assert await running == "cancelled"
        assert await app.turn("next") == "completed"

    asyncio.run(go())
    assert "! stopped" in term.text
    assert tool_results(app.session) == [CANCELLED_RESULT]
    roles = [m.role for m in provider.requests[1].messages]
    assert roles == ["user", "assistant", "tool", "user"]


def test_sigint_under_asyncio_run_stops_only_the_turn(tmp_path):
    """The real Ctrl+C path: asyncio.run turns SIGINT into cancelling the main task."""
    import signal

    term = FakeTerm()
    turns = [Turn(calls=[("hang", {})]), Turn(calls=[("hang", {})]), Turn(text="after")]
    tool = HangTool()
    app, _ = make_app(tmp_path, turns, term, extra_tools=[tool])

    async def main():
        loop = asyncio.get_running_loop()
        statuses = []
        for task in ("one", "two"):  # Ctrl+C twice: each stops only its turn
            tool.started.clear()
            running = asyncio.ensure_future(app.turn(task))
            await asyncio.wait_for(tool.started.wait(), 2)
            # Raised in the main thread, as a terminal's Ctrl+C is delivered to it.
            loop.call_later(0.05, signal.raise_signal, signal.SIGINT)
            statuses.append(await running)
        statuses.append(await app.turn("three"))
        return statuses

    before = signal.getsignal(signal.SIGINT)
    assert asyncio.run(main()) == ["cancelled", "cancelled", "completed"]
    assert term.text.count("! stopped") == 2
    assert signal.getsignal(signal.SIGINT) is before


def test_resume_and_sessions(tmp_path):
    term = FakeTerm(["remember me", "/quit"])
    app, _ = make_app(tmp_path, [Turn(text="noted")], term)
    asyncio.run(app.run())
    saved = app.session.session_id

    term2 = FakeTerm(["/sessions", "/resume nope", "/resume last", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    assert saved in term2.text
    assert "remember me" in term2.text
    assert "no session 'nope'" in term2.text
    assert app2.session.session_id == saved
    assert [m.content for m in app2.session.messages] == ["remember me", "noted"]


def test_diff(tmp_path):
    term = FakeTerm(["/diff", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    ws = app.session.workspace
    asyncio.run(app.run())
    assert "not a git repository" in term.text

    def git(*args):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)

    git("init", "-q")
    (ws / "a.txt").write_text("old\n")
    git("add", "a.txt")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    term = FakeTerm(["/diff", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    asyncio.run(app.run())
    assert "no changes" in term.text
    (ws / "a.txt").write_text("new\n")
    (ws / "b.txt").write_text("fresh\n")
    term = FakeTerm(["/diff", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    asyncio.run(app.run())
    assert "-old" in term.text
    assert "+new" in term.text
    assert "untracked:" in term.text
    assert "b.txt" in term.text


def test_render_stream_and_events(tmp_path):
    term = FakeTerm()
    app, _ = make_app(tmp_path, [], term)
    app.render(ev.TextDelta("hel"))
    app.render(ev.TextDelta("lo"))
    app.render(ev.AssistantMessage(step=1, text="hello"))
    assert term.text == "hello\n"  # streamed text is not printed twice
    app.show_reasoning = True
    app.render(ev.ReasoningDelta("hmm"))
    app.render(ev.ToolStarted(call_id="1", name="bash", arguments='{"command": "ls -la"}'))
    app.render(
        ev.ToolFinished(
            call_id="1", name="bash", is_error=True, output="a\nboom\n", duration_ms=1500
        )
    )
    app.render(ev.ToolStarted(call_id="a:1", name="agent-1/read", arguments='{"file_path": "x"}'))
    app.render(
        ev.ToolFinished(
            call_id="a:1",
            name="agent-1/read",
            is_error=False,
            output="1: a\n2: b\n",
            duration_ms=10,
        )
    )
    app.render(ev.Retry(attempt=2, delay_s=4, error="429"))
    app.render(ev.ErrorEvent(code="x", message="bad"))
    text = term.text
    assert "thinking: hmm\n" in text
    assert "● bash ls -la  ✗ boom · 1.5s\n" in text
    assert "    ● agent-1/read x  ✓ 2 lines · 0.0s\n" in text
    assert "retry 2 in 4s: 429" in text
    assert "✗ error: bad" in text


def test_progress_follows_parallel_calls_and_keeps_output_compact(tmp_path):
    term = FakeTerm()
    app, _ = make_app(tmp_path, [], term)
    app._turn_started = 0
    app.render(ev.ToolStarted("a", "grep", "{}"))
    app.render(ev.ToolStarted("b", "read", "{}"))
    assert "Running grep, read" in app._progress()
    app.render(ev.ToolFinished("a", "grep", False, "x" * 50_000 + "\nsecond line", 10))
    assert "Running read" in app._progress()
    app.render(ev.ToolFinished("b", "read", False, "one\ntwo\n", 10))
    assert "Model thinking" in app._progress()
    assert len(term.text) < 240
    assert "second line" not in term.text
    assert "2 lines" in term.text


def test_stream_retains_deltas_and_ignores_non_printed_events(tmp_path):
    term = FakeTerm()
    app, _ = make_app(tmp_path, [], term)
    app.render(ev.TextDelta("hello "))
    assert term.text == "hello "
    app.render(ev.UsageUpdate(1, {}, {}, None, None))
    app.render(ev.TextDelta("world\n"))
    app.render(ev.AssistantMessage(step=1, text="hello world\n"))
    assert term.text == "hello world\n"
    app.render(ev.TextDelta("partial response"))
    app.render(ev.Retry(attempt=2, delay_s=1, error="retry"))
    app.render(ev.AssistantMessage(step=2, text="complete response"))
    assert term.text.endswith("complete response\n")


def test_helpers():
    assert Palette(True)("red", "x") == "\x1b[31mx\x1b[0m"
    assert Palette(False)("red", "x") == "x"
    assert _glyphs("cp1252")["tool"] == "*"
    assert _glyphs("utf-8")["tool"] == "●"
    assert brief('{"command": "a  b\\nc"}') == "a b c"
    assert brief("x" * 200, limit=10) == "xxxxxxx..."


def test_cli_chat_needs_model(monkeypatch, capsys):
    monkeypatch.delenv("KAROX_MODEL", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "--model", ""])
    assert exc.value.code == 2
    assert cli.main([]) == 0  # not a TTY under pytest: help, not the chat
    assert "chat" in capsys.readouterr().out


def test_resume_last_skips_empty_and_current_sessions(tmp_path):
    from karox6.core.store import SessionStore

    term = FakeTerm(["used", "/quit"])
    app, _ = make_app(tmp_path, [Turn(text="ok")], term)
    asyncio.run(app.run())
    used = app.session.session_id
    # A newer-looking, never-used session (what every chat start creates).
    term2 = FakeTerm(["/resume last", "/quit"])
    app2, _ = make_app(tmp_path, [], term2, session_kwargs={"session_id": "99999999-999999-ffffff"})
    assert SessionStore.latest(app2.config.home) == used
    asyncio.run(app2.run())
    assert app2.session.session_id == used


def test_cli_reaches_prompt_without_runtime_setup(tmp_path, monkeypatch):
    from karox6 import tui

    settings = tmp_path / "config.toml"
    settings.write_text('model = "configured"\n', encoding="utf-8")
    monkeypatch.setenv("KAROX_CONFIG", str(settings))
    monkeypatch.delenv("KAROX_MODEL", raising=False)
    captured = []
    lines = iter(("/help", "/clear", "/quit"))

    async def read(prompt):
        captured.append(prompt)
        return next(lines)

    def unexpected(*args, **kwargs):
        pytest.fail("runtime loaded before a task or session command")

    def terminal_io(history, *, workspace, home):
        assert workspace == home == tmp_path.resolve()
        assert history == home / "chat_history"
        return read, read

    monkeypatch.setattr(tui, "terminal_io", terminal_io)
    monkeypatch.setattr(tui, "stdout_write", captured.append)
    monkeypatch.setattr(tui, "secret_reader", unexpected)
    monkeypatch.setattr(cli, "_provider", unexpected)
    monkeypatch.setattr(cli, "_mcp_tools", unexpected)
    assert cli.main(["chat", "--home", str(tmp_path), "--cwd", str(tmp_path)]) == 0
    assert "configured" in captured[0]
    assert captured.count(tui._glyphs(sys.stdout.encoding)["prompt"]) == 3
    assert not (tmp_path / "sessions").exists()


def test_cli_initializes_after_input_and_reuses_runtime(tmp_path, monkeypatch, capsys):
    from karox6 import tui

    timeline = []
    tasks = iter(("first task", "second task", "/quit"))
    provider = ScriptedProvider([Turn(text="first reply"), Turn(text="second reply")])

    async def read(prompt):
        task = next(tasks)
        timeline.append(task)
        return task

    def create_provider(args):
        timeline.append("provider")
        return provider

    async def connect(args, home, *, warn):
        timeline.append("mcp")
        warn("server unavailable\x1b[2J\n")
        return [], ["fake handle"]

    async def close(handles):
        assert handles == ["fake handle"]
        timeline.append("closed")

    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setattr(tui, "terminal_io", lambda history, **kwargs: (read, read))
    monkeypatch.setattr(cli, "_provider", create_provider)
    monkeypatch.setattr(cli, "_mcp_tools", connect)
    monkeypatch.setattr(cli, "_close_mcp", close)
    assert cli.main(["chat", "--model", "test", "--home", str(tmp_path)]) == 0
    assert timeline == ["first task", "provider", "mcp", "second task", "/quit", "closed"]
    capture = capsys.readouterr()
    assert "server unavailable\n" in capture.out
    assert "\x1b" not in capture.out
    assert capture.err == ""
    assert [m.content for m in provider.requests[1].messages] == [
        "first task",
        "first reply",
        "second task",
    ]


def test_cancelled_initialization_keeps_one_session_factory(tmp_path, monkeypatch):
    term = FakeTerm()
    provider = ScriptedProvider([Turn(text="after cancellation")])
    initializes = []

    async def initialize(app):
        initializes.append(app)
        return {}

    app = ChatApp(
        config=SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home"),
        provider=provider,
        read_line=term.read_line,
        write=term.write,
        initialize=initialize,
    )
    new_session = app._new_session
    release = threading.Event()
    built = []

    async def go():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def create(**kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            built.append(new_session(**kwargs))
            return built[-1]

        monkeypatch.setattr(app, "_new_session", create)
        first = asyncio.create_task(app.turn("cancelled during preparation"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            first.cancel()
            assert await asyncio.wait_for(first, 2) == "cancelled"
        finally:
            release.set()
        assert await app.turn("continue") == "completed"

    asyncio.run(go())
    assert initializes == [app]
    assert built == [app.session]
    assert provider.requests[0].messages[0].content == "continue"


@pytest.mark.parametrize("encoding", ["utf-8", "cp1251"])
def test_piped_chat_decodes_cyrillic_and_emits_no_terminal_controls(tmp_path, encoding):
    code = r"""
import sys
from karox6 import cli
from karox6.providers.scripted import ScriptedProvider, Turn
provider = ScriptedProvider([Turn(text="\x1b[31mОтвет\x1b[0m")])
cli._provider = lambda args: provider
assert cli.main(["chat", "--model", "test", "--no-mcp", "--home", sys.argv[1]]) == 0
assert provider.requests[0].messages[0].content == "Привет мир"
"""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1251",
        "KAROX_CONFIG": str(tmp_path / "missing.toml"),
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        input="Привет мир\r\n".encode(encoding),
        env=env,
        capture_output=True,
        check=True,
    )
    assert "Ответ" in result.stdout.decode().splitlines()
    assert b"\x1b" not in result.stdout


def test_legacy_console_output_and_no_color(monkeypatch):
    import io

    from karox6.tui import color_enabled, stdout_write

    class LegacyConsole(io.TextIOWrapper):
        def isatty(self):
            return True

    stream = LegacyConsole(io.BytesIO(), encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("NO_COLOR", "1")
    cli.safe_streams()
    assert not color_enabled()
    stdout_write("Кириллица🙂")
    assert stream.buffer.getvalue().decode("cp1251") == "Кириллица?"


def test_update_rejects_a_typo_instead_of_installing(tmp_path, monkeypatch):
    """`/update chek` used to mean force=False check_only=False — a full network install."""
    from karox6 import update

    ran = []
    monkeypatch.setattr(update, "run_update", lambda *a, **k: ran.append((a, k)))
    term = FakeTerm(["/update chek", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    asyncio.run(app.run())
    assert ran == []
    assert "unknown /update argument 'chek'" in term.text
    assert "/update check or /update force" in term.text


def test_update_paints_no_terminal_controls(tmp_path, monkeypatch):
    """Updater output is remote bytes (commit subjects, pip/git stderr, versions): the
    progress callback and the error line must not let CSI/OSC reach the terminal."""
    from karox6 import update

    def hostile(source, out, **_kwargs):
        out("fetching \x1b]8;;https://evil.test\x1b\\repo\x1b[2J")
        raise update.UpdateError("git said:\n  \x1b[31mfatal\x1b[0m\n  \x1b]0;pwned\x07 boom")

    monkeypatch.setattr(update, "run_update", hostile)
    term = FakeTerm(["/update", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    asyncio.run(app.run())
    body = term.text.split("Type a task to start.", 1)[-1]
    assert "\x1b" not in body
    assert "\x07" not in body
    assert "  fetching repo\n" in body
    assert "update failed: git said:\n  fatal\n   boom" in body


def test_quit_aborts_a_running_update(tmp_path, monkeypatch):
    """The updater's child processes (a 600s clone, a 900s pip) used to keep the process
    hung after a quit: close() must call update.abort() while an update is in flight."""
    from karox6 import update

    aborted = []
    monkeypatch.setattr(update, "abort", lambda: aborted.append(True))
    term = FakeTerm()
    app, _ = make_app(tmp_path, [], term)
    app._updating = True  # /update is running in its executor thread
    asyncio.run(app.close())
    assert aborted == [True]


def test_sessions_list_never_replays_control_sequences_from_a_title(tmp_path):
    term = FakeTerm(["hi \x1b]0;pwned\x07\x1b[2Jthere", "/quit"])
    app, _ = make_app(tmp_path, [Turn(text="ok")], term)
    asyncio.run(app.run())

    term2 = FakeTerm(["/sessions", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    line = next(x for x in term2.text.splitlines() if app.session.session_id in x)
    assert "hi " in line
    assert "there" in line
    assert "\x1b]0" not in line
    assert "\x1b[2J" not in line


def test_resume_shows_the_conversation_where_it_stopped(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "notes.txt").write_text("alpha\n")
    first = [Turn(calls=[("read", {"file_path": "notes.txt"})]), Turn(text="It says alpha.")]
    term = FakeTerm(["what is in notes.txt?", "/quit"])
    app, _ = make_app(tmp_path, first, term)
    asyncio.run(app.run())

    term2 = FakeTerm(["/resume last", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    shown = term2.text
    assert "› what is in notes.txt?" in shown
    assert "read notes.txt" in shown
    assert "It says alpha." in shown
    assert shown.index("what is in notes.txt?") < shown.index("It says alpha.")
    assert shown.index("It says alpha.") < shown.index("resumed ")

    # --resume at start shows it before the first prompt; only the last turns are shown
    term3 = FakeTerm(["/quit"])
    app3, _ = make_app(tmp_path, [], term3)
    app3._initialize = _resume_last(tmp_path)
    app3.replay_on_start = True
    asyncio.run(app3.run())
    assert "› what is in notes.txt?" in term3.text


def _resume_last(tmp_path):
    from karox6.core.store import resume_kwargs

    async def init(app):
        return resume_kwargs(tmp_path / "home", "last", workspace=tmp_path / "ws")

    return init


def test_replay_keeps_only_the_last_exchanges(tmp_path):
    turns = [Turn(text=f"answer {n}") for n in range(13)]
    term = FakeTerm([f"question {n}" for n in range(13)] + ["/quit"])
    app, _ = make_app(tmp_path, turns, term)
    asyncio.run(app.run())
    term2 = FakeTerm(["/resume last", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    assert "… 6 earlier messages not shown" in term2.text
    assert "› question 2\n" not in term2.text
    assert "› question 3" in term2.text
    assert "answer 12" in term2.text


def test_effort_command_changes_the_next_request(tmp_path):
    term = FakeTerm(["/effort high", "first", "/effort bogus", "/effort off", "second", "/quit"])
    app, provider = make_app(tmp_path, [Turn(text="one"), Turn(text="two")], term)
    asyncio.run(app.run())
    assert [r.reasoning_effort for r in provider.requests] == ["high", None]
    assert "reasoning effort: high" in term.text
    assert "usage: /effort [low|medium|high|xhigh|max|off]" in term.text
    assert "reasoning effort: provider default" in term.text


def test_replay_shows_a_failed_command_as_failed(tmp_path):
    turns = [Turn(calls=[("bash", {"command": "exit 3"})]), Turn(text="it failed")]
    term = FakeTerm(["run it", "/quit"])
    app, _ = make_app(tmp_path, turns, term)
    asyncio.run(app.run())
    assert "✗ exit 3" in term.text
    term2 = FakeTerm(["/resume last", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    assert "✗ exit 3" in term2.text


def test_export_writes_the_conversation_as_markdown(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "notes.txt").write_text("alpha\n")
    turns = [Turn(calls=[("read", {"file_path": "notes.txt"})]), Turn(text="It says alpha.")]
    asked = "what is in \x1b]0;x\x07notes.txt?"
    term = FakeTerm(["/export", asked, "/export out/chat.md", "/quit"])
    app, _ = make_app(tmp_path, turns, term)
    asyncio.run(app.run())
    assert "nothing to export yet" in term.text
    text = (tmp_path / "ws" / "out" / "chat.md").read_text(encoding="utf-8")
    assert text.startswith(f"# KaroX session {app.session.session_id}")
    assert "## You\n\nwhat is in notes.txt?" in text
    assert "- `read` notes.txt" in text
    assert "## KaroX\n\nIt says alpha." in text
    assert "\x1b" not in text


def test_switching_provider_moves_the_bridge_agent_with_its_key(tmp_path):
    term = FakeTerm(["/quit"])
    old = ["--provider", "openai-chat", "--model", "test", "--base-url", "http://a.test/v1"]
    app, _ = make_app(
        tmp_path,
        [],
        term,
        bridge_agent_args=[*old, "--api-key-env", "KAROX_BRIDGE_API_KEY"],
        bridge_env={"KAROX_BRIDGE_API_KEY": "sk-a"},
    )
    assert asyncio.run(app.run()) == 0
    asyncio.run(app._use("anthropic", "https://b.test", "claude-x", "sk-b", "b"))
    assert app.bridge_agent_args == [
        "--provider",
        "anthropic",
        "--model",
        "claude-x",
        "--base-url",
        "https://b.test",
        "--api-key-env",
        "KAROX_BRIDGE_API_KEY",
    ]
    assert app.bridge_env == {"KAROX_BRIDGE_API_KEY": "sk-b"}
    asyncio.run(app._use("openai-chat", "http://local.test/v1", "q", None, "local"))
    assert "--api-key-env" not in app.bridge_agent_args
    assert app.bridge_env == {}  # no key goes to a server that was given none
    app._switch_model("q2")
    assert app.bridge_agent_args[3] == "q2"


def test_the_models_words_stand_apart_from_the_tool_lines(tmp_path):
    term = FakeTerm(["look", "/quit"])
    turns = [
        Turn(text="Let me look first.", calls=[("read", {"file_path": "notes.txt"})]),
        Turn(text="It says alpha."),
    ]
    app, _ = make_app(tmp_path, turns, term)
    (tmp_path / "ws" / "notes.txt").write_text("alpha\n")
    assert asyncio.run(app.run()) == 0
    text = term.text
    words = text.index("Let me look first.")
    tool = text.index("read notes.txt")
    between = text[words + len("Let me look first.") : tool]
    assert between.count("\n") >= 2  # a blank line before what the model ran
    assert "It says alpha." in text


def test_skills_and_slash_commands_in_the_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nobody"))
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    ws = tmp_path / "ws"
    skill = ws / ".karox" / "skills" / "fix-bugs"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: fix-bugs\ndescription: find and fix\n---\nHunt.\n")
    (ws / ".karox" / "commands").mkdir()
    (ws / ".karox" / "commands" / "ship.md").write_text("Ship $ARGUMENTS today.\n")
    term = FakeTerm(["/skills", "/ship v2", "/fix-bugs the parser", "/fix-bug", "/quit"])
    app, provider = make_app(tmp_path, [Turn(text="shipped"), Turn(text="fixed")], term)
    app.config.skills = True
    asyncio.run(app.run())
    assert "/fix-bugs" in term.text
    assert "/ship" in term.text
    sent = [r.messages[-1].content for r in provider.requests]
    assert "Ship v2 today." in sent[0]
    assert "Hunt." in sent[1]
    assert sent[1].endswith("Arguments: the parser")
    assert "did you mean" in term.text  # /fix-bug is close to /fix-bugs
