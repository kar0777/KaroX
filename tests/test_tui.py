import asyncio
import subprocess

import pytest

from karox6 import cli
from karox6.core import events as ev
from karox6.core.session import CANCELLED_RESULT, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ApprovalRequest, ToolResult
from karox6.tui import ChatApp, Palette, _glyphs, brief


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

    async def run(self, args, ctx):
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
    assert term.text.count("[completed]") == 2
    seen = [m.content for m in provider.requests[1].messages]
    assert seen == ["hello", "first answer", "and again"]


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
    app, provider = make_app(tmp_path, turns, term, extra_tools=[HangTool()])

    async def go():
        running = asyncio.create_task(app.turn("start"))
        for _ in range(500):
            if "hang" in term.text:
                break
            await asyncio.sleep(0.01)
        running.cancel()  # what Ctrl+C does to the main task
        assert await running == "cancelled"
        assert await app.turn("next") == "completed"

    asyncio.run(go())
    assert "[stopped]" in term.text
    assert tool_results(app.session) == [CANCELLED_RESULT]
    roles = [m.role for m in provider.requests[1].messages]
    assert roles == ["user", "assistant", "tool", "user"]


def test_sigint_under_asyncio_run_stops_only_the_turn(tmp_path):
    """The real Ctrl+C path: asyncio.run turns SIGINT into cancelling the main task."""
    import signal
    import threading

    term = FakeTerm()
    turns = [Turn(calls=[("hang", {})]), Turn(calls=[("hang", {})]), Turn(text="after")]
    app, _ = make_app(tmp_path, turns, term, extra_tools=[HangTool()])

    async def main():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        original = term.write

        def write(text):
            original(text)
            if "hang" in text:
                loop.call_soon_threadsafe(started.set)

        app.write = write
        statuses = []
        for task in ("one", "two"):  # Ctrl+C twice: each stops only its turn
            started.clear()
            running = asyncio.ensure_future(app.turn(task))
            await started.wait()
            threading.Timer(0.05, signal.raise_signal, args=(signal.SIGINT,)).start()
            statuses.append(await running)
        statuses.append(await app.turn("three"))
        return statuses

    before = signal.getsignal(signal.SIGINT)
    assert asyncio.run(main()) == ["cancelled", "cancelled", "completed"]
    assert term.text.count("[stopped]") == 2
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
    app.render(ev.Retry(attempt=2, delay_s=4, error="429"))
    app.render(ev.ErrorEvent(code="x", message="bad"))
    text = term.text
    assert "thinking: hmm\n" in text
    assert "● bash ls -la" in text
    assert "✗ 1.5s boom" in text
    assert "retry 2 in 4s: 429" in text
    assert "error: bad" in text


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
