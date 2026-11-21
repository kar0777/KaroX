import asyncio
import subprocess

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.defaults import DummyOutput

from karox6.core import events as ev
from karox6.core.digest import brief, is_tool_failure, outcome
from karox6.core.messages import Message, Price, Usage
from karox6.core.session import SessionConfig
from karox6.providers.base import ProviderError
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tui import (
    COMMANDS,
    ChatApp,
    build_completer,
    build_prompt_session,
    expand_file_mentions,
)


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


def make_app(tmp_path, turns, term, *, config_kwargs=None, **kwargs):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = SessionConfig(model="test", workspace=ws, home=tmp_path / "home", **(config_kwargs or {}))
    provider = ScriptedProvider(list(turns))
    app = ChatApp(
        config=cfg,
        provider=provider,
        read_line=term.read_line,
        read_answer=term.read_answer,
        write=term.write,
        **kwargs,
    )
    return app, provider


def _complete(completer, text):
    doc = Document(text, cursor_position=len(text))
    return list(completer.get_completions(doc, CompleteEvent()))


# -- completion ------------------------------------------------------------------------


def test_slash_command_and_argument_completion(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    # Save a session so /resume completion can suggest it.
    term = FakeTerm(["fix login flow", "/quit"])
    app, _ = make_app(tmp_path, [Turn(text="done")], term)
    asyncio.run(app.run())
    saved_id = app.session.session_id

    completer = build_completer(workspace=ws, home=home)

    all_cmds = _complete(completer, "/")
    names = [c.text for c in all_cmds]
    assert names == [spec.name for spec in COMMANDS]
    assert all(c.display_meta_text for c in all_cmds)

    co = {c.text: c.display_meta_text for c in _complete(completer, "/co")}
    assert set(co) == {"/cost", "/compact"}
    assert "token" in co["/cost"].lower()

    ex = [c.text for c in _complete(completer, "/ex")]
    assert ex == ["/exit"]

    approve = {c.text: c.display_meta_text for c in _complete(completer, "/approve ")}
    assert set(approve) == {"ask", "allow", "deny"}
    assert all(approve.values())

    bridge = [c.text for c in _complete(completer, "/bridge s")]
    assert bridge == ["stop"]

    reasoning = [c.text for c in _complete(completer, "/reasoning ")]
    assert reasoning == ["show", "hide"]

    resumes = {c.text: c.display_meta_text for c in _complete(completer, "/resume ")}
    assert "last" in resumes
    assert saved_id in resumes
    assert "fix login flow" in resumes[saved_id]


def test_at_file_mention_completion(tmp_path, monkeypatch):
    from karox6.tools import search

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (ws / "notes file.md").write_text("# notes\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "junk.pyc").write_bytes(b"x")

    scans = []
    original_list_files = search._list_files

    def list_files(root, *, include_ignored):
        scans.append(root)
        return original_list_files(root, include_ignored=include_ignored)

    monkeypatch.setattr(search, "_list_files", list_files)
    completer = build_completer(workspace=ws)
    assert _complete(completer, "/he")[0].text == "/help"
    assert scans == []

    all_files = [c.text for c in _complete(completer, "check @")]
    assert scans == [ws]
    assert "@src/app.py" in all_files
    assert '@"notes file.md"' in all_files
    assert not any("__pycache__" in item for item in all_files)

    by_base = _complete(completer, "edit @app")
    assert by_base[0].text == "@src/app.py"
    assert by_base[0].display_meta_text  # file size hint

    # Emails must not trigger @file completion.
    assert _complete(completer, "mail user@app") == []


# -- @path attachment ------------------------------------------------------------------


def test_file_mention_expansion_and_safety(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "my file.txt").write_text("hello spaces\n", encoding="utf-8")
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02")
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret\n", encoding="utf-8")

    res = expand_file_mentions(
        'Check @src/a.py, @"my file.txt", @src/a.py, @dataclass and user@example.com.',
        ws,
    )
    assert [rel for rel, *_ in res.attached] == ["src/a.py", "my file.txt"]
    assert res.warnings == []
    assert '<attached_file path="src/a.py">\nx = 1\n\n</attached_file>' in res.text
    assert '<attached_file path="my file.txt">\nhello spaces\n\n</attached_file>' in res.text
    assert res.text.count('<attached_file path="src/a.py">') == 1  # deduplicated

    bad = expand_file_mentions("Read @../secret.txt @src @bin.dat", ws)
    assert bad.attached == []
    assert any("outside workspace" in w for w in bad.warnings)
    assert any("directory" in w for w in bad.warnings)
    assert any("binary file" in w for w in bad.warnings)

    # Truncation cap
    monkeypatch.setattr("karox6.tui.MENTION_MAX_BYTES", 10)
    big = expand_file_mentions("Look at @my file.txt and @src/a.py", ws)
    (ws / "big.txt").write_text("a" * 50, encoding="utf-8")
    trunc = expand_file_mentions("Look at @big.txt", ws)
    assert trunc.attached[0][3] is True
    assert "truncated" in trunc.text
    assert big.warnings == []


def test_turn_attaches_mentioned_file_and_observes_it(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "hello.py"
    target.write_text("VALUE = 42\n", encoding="utf-8")
    term = FakeTerm(["Explain @hello.py", "/quit"])
    app, provider = make_app(tmp_path, [Turn(text="It sets VALUE.")], term)

    asyncio.run(app.run())

    sent = provider.requests[0].messages[0].content
    assert '<attached_file path="hello.py">' in sent
    assert "VALUE = 42" in sent
    assert "attached: hello.py" in term.text
    assert app.session.files.seen(target)


# -- prompt_toolkit multiline, paste, history & Ctrl+R ---------------------------------


def test_prompt_session_multiline_paste_and_ctrl_r_history(tmp_path):
    hist_path = tmp_path / "home" / "chat_history"
    ws = tmp_path / "ws"
    ws.mkdir()

    async def scenario():
        with create_pipe_input() as inp:
            sess = build_prompt_session(
                history=hist_path, workspace=ws, input=inp, output=DummyOutput()
            )
            # Alt+Enter inserts a newline; Enter submits.
            t1 = asyncio.create_task(sess.prompt_async("> "))
            await asyncio.sleep(0.02)
            inp.send_text("first line\x1b\rsecond line\r")
            assert await t1 == "first line\nsecond line"

            # Ctrl+J inserts a newline; Enter submits.
            t2 = asyncio.create_task(sess.prompt_async("> "))
            await asyncio.sleep(0.02)
            inp.send_text("alpha\nbeta\r")
            assert await t2 == "alpha\nbeta"

            # Bracketed paste stays one single message.
            t3 = asyncio.create_task(sess.prompt_async("> "))
            await asyncio.sleep(0.02)
            inp.send_text("\x1b[200~p1\np2\np3\x1b[201~\r")
            assert await t3 == "p1\np2\np3"

            # Trailing backslash + Enter continues on next line.
            t4 = asyncio.create_task(sess.prompt_async("> "))
            await asyncio.sleep(0.02)
            inp.send_text("part1\\\rpart2\r")
            assert await t4 == "part1\npart2"

        # New session loads persistent FileHistory and Ctrl+R searches it.
        with create_pipe_input() as inp2:
            sess2 = build_prompt_session(
                history=hist_path, workspace=ws, input=inp2, output=DummyOutput()
            )
            t5 = asyncio.create_task(sess2.prompt_async("> "))
            await asyncio.sleep(0.05)
            inp2.send_text("\x12alpha\r\r")
            assert await t5 == "alpha\nbeta"

    asyncio.run(scenario())
    assert hist_path.is_file()


# -- /help, /clear vs /new, /cost, /status, /compact, /reasoning -----------------------


def test_grouped_help_and_reasoning_args(tmp_path):
    term = FakeTerm(["/help", "/reasoning show", "/reasoning hide", "/quit"])
    app, _ = make_app(tmp_path, [], term)
    asyncio.run(app.run())

    for heading in (
        "Session",
        "Workspace & Context",
        "Model & Setup",
        "Tools & Bridge",
        "Input & Shortcuts",
    ):
        assert heading in term.text
    for cmd in ("/clear", "/cost", "/compact", "@path", "Ctrl+R", "Alt+Enter", "Esc or Ctrl+C"):
        assert cmd in term.text
    assert "reasoning shown\n" in term.text
    assert "reasoning hidden\n" in term.text
    assert app.show_reasoning is False


def test_clear_keeps_session_while_new_resets(tmp_path):
    term = FakeTerm(["hello", "/clear", "/new", "/quit"])
    app, _ = make_app(tmp_path, [Turn(text="hi back")], term)

    async def run():
        await app.turn("hello")
        sid_before = app.session.session_id
        msgs_before = list(app.session.messages)
        await app.command("/clear")
        assert app.session.session_id == sid_before
        assert app.session.messages == msgs_before
        assert "\x1b[2J\x1b[H" in term.text
        assert f"session {sid_before} kept (2 messages)" in term.text

        await app.command("/new")
        assert app.session.session_id != sid_before
        assert app.session.messages == []

    asyncio.run(run())


def test_cost_and_status_show_cached_tokens_context_and_price(tmp_path):
    turn = Turn(
        text="answer",
        usage=Usage(input_tokens=1200, cached_tokens=500, output_tokens=300, reasoning_tokens=80),
    )
    term = FakeTerm(["hi", "/status", "/cost", "/quit"])
    app, _ = make_app(
        tmp_path,
        [turn],
        term,
        config_kwargs={"price": Price(input=2.0, output=10.0, cached=0.5)},
    )
    asyncio.run(app.run())

    assert "1200 in (500 cached) / 300 out" in term.text
    assert "context    " in term.text
    assert "requests   1" in term.text
    assert "1,200 (500 cached)" in term.text
    assert "300 (80 reasoning)" in term.text
    assert "1,500 tokens" in term.text
    assert "cost       $0.0046" in term.text

    # Without pricing configured, /cost explains how to configure it.
    term2 = FakeTerm(["/cost", "/quit"])
    app2, _ = make_app(tmp_path, [], term2)
    asyncio.run(app2.run())
    assert "not configured" in term2.text


def test_compact_command_wires_core_compaction_and_handles_errors(tmp_path):
    # Too short to compact.
    term_short = FakeTerm(["/compact", "/quit"])
    app_short, _ = make_app(tmp_path, [], term_short)
    asyncio.run(app_short.run())
    assert "nothing to compact yet" in term_short.text

    # Multi-turn history gets compacted via Session.compact().
    history = [
        Message("user", "task one"),
        Message("assistant", "step one " * 40),
        Message("user", "task two"),
        Message("assistant", "step two " * 40),
    ]
    term = FakeTerm(["/compact", "/quit"])
    app, _ = make_app(
        tmp_path,
        [Turn(text="## Primary Request and Intent\n- summary")],
        term,
        session_kwargs={"messages": history},
    )
    asyncio.run(app.run())
    assert "compacting context..." in term.text
    assert "context compacted (summary):" in term.text
    assert any(m.meta.get("kind") == "compaction" for m in app.session.messages)

    # Provider errors during /compact are displayed cleanly.
    term_err = FakeTerm(["/compact", "/quit"])
    app_err, _ = make_app(
        tmp_path,
        [Turn(error=ProviderError("bad_request", "service rejected"))],
        term_err,
        session_kwargs={"messages": list(history)},
    )
    asyncio.run(app_err.run())
    assert "compaction failed: bad_request: service rejected" in term_err.text


# -- readable output: compact tool lines, exit codes, diff stats, answer separation ----


def test_compact_tool_output_exit_codes_parallel_and_answer_separation(tmp_path):
    term = FakeTerm()
    app, _ = make_app(tmp_path, [], term, color=True)

    # Non-zero shell exit code (is_error=False in ShellTool) renders as failure in red.
    app.render(ev.ToolStarted(call_id="1", name="bash", arguments='{"command": "pytest -q"}'))
    app.render(
        ev.ToolFinished(
            call_id="1",
            name="bash",
            is_error=False,
            output="1 failed\n[exit code: 1]",
            duration_ms=1400,
            meta={"exit_code": 1},
        )
    )
    # Edit shows coloured +N -M diff stats on one line.
    app.render(
        ev.ToolStarted(
            call_id="2",
            name="edit",
            arguments='{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2\\ny = 3"}',
        )
    )
    app.render(
        ev.ToolFinished(
            call_id="2",
            name="edit",
            is_error=False,
            output="Edited a.py: replaced 1 occurrence.",
            duration_ms=20,
        )
    )
    # Parallel reads each get one clean line.
    app.render(ev.ToolStarted(call_id="3", name="read", arguments='{"file_path": "a.py"}'))
    app.render(ev.ToolStarted(call_id="4", name="read", arguments='{"file_path": "b.py"}'))
    app.render(
        ev.ToolFinished(
            call_id="3",
            name="read",
            is_error=False,
            output="<content>1: x\n</content>",
            duration_ms=10,
            meta={"lines": [1, 10], "total_lines": 10},
        )
    )
    app.render(
        ev.ToolFinished(
            call_id="4",
            name="read",
            is_error=False,
            output="<content>1: y\n</content>",
            duration_ms=15,
            meta={"lines": [1, 5], "total_lines": 25},
        )
    )
    # Final answer is separated by a blank line after tools.
    app.render(ev.TextDelta("All fixed."))
    app.render(ev.AssistantMessage(step=2, text="All fixed."))

    plain = term.text
    assert "\x1b[31m✗\x1b[0m" in plain
    assert "\x1b[31mexit 1 · 1 failed\x1b[0m" in plain
    assert "\x1b[32m+2\x1b[0m \x1b[31m-1\x1b[0m" in plain
    assert "10 lines" in plain
    assert "lines 1-5 of 25" in plain
    assert "\n\nAll fixed.\n" in plain


def test_digest_helpers_cover_read_glob_grep_and_shell():
    assert brief('{"file_paths": ["a.py", "b.py"]}') == "a.py, b.py"
    fail_shell = ev.ToolFinished(
        call_id="1",
        name="bash",
        is_error=False,
        output="oops\n[exit code: 2]",
        duration_ms=10,
        meta={"exit_code": 2},
    )
    assert is_tool_failure(fail_shell) is True
    assert outcome(fail_shell) == "exit 2 · oops"

    ok_shell = ev.ToolFinished(
        call_id="2",
        name="bash",
        is_error=False,
        output="ok\n[exit code: 0]",
        duration_ms=10,
        meta={"exit_code": 0},
    )
    assert is_tool_failure(ok_shell) is False
    assert outcome(ok_shell) == "exit 0 · ok"

    assert (
        outcome(
            ev.ToolFinished(
                call_id="3", name="glob", is_error=False, output="a.py\nb.py\n", duration_ms=5
            )
        )
        == "2 files"
    )
    assert (
        outcome(
            ev.ToolFinished(
                call_id="4",
                name="grep",
                is_error=False,
                output="a.py\nLine 1: foo\nLine 9: bar\n",
                duration_ms=5,
            )
        )
        == "2 matches"
    )


def test_colored_diff_summary_and_lines(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)

    git("init", "-q")
    (ws / "a.txt").write_text("old\n", encoding="utf-8")
    git("add", "a.txt")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    (ws / "a.txt").write_text("new1\nnew2\n", encoding="utf-8")

    term = FakeTerm(["/diff", "/quit"])
    app, _ = make_app(tmp_path, [], term, color=True)
    asyncio.run(app.run())

    assert "1 file changed" in term.text
    assert "\x1b[32m+2\x1b[0m" in term.text
    assert "\x1b[31m-1\x1b[0m" in term.text
    assert "\x1b[31m-old\x1b[0m" in term.text
    assert "\x1b[32m+new1\x1b[0m" in term.text
    assert "\x1b[36m@@" in term.text
