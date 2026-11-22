import asyncio

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from karox6.core.session import SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ApprovalRequest, ToolResult
from karox6.tui import ChatApp, terminal_io
from karox6.tui_terminal import TurnDisplay


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.frames = []
        self.size = Size(rows=24, columns=80)
        self.rendered_sizes = set()

    def write(self, text):
        self.frames.append(text)

    def get_size(self):
        return self.size

    def get_rows_below_cursor_position(self):
        return self.size.rows

    def flush(self):
        self.rendered_sizes.add(self.size)


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


def test_live_status_and_partial_text_leave_clean_scrollback():
    visible = RecordingOutput()
    scrollback = []
    elapsed = 0

    async def go():
        nonlocal elapsed
        with create_pipe_input() as keys, create_app_session(input=keys, output=visible):
            async with TurnDisplay(
                scrollback.append, lambda: f"thinking {elapsed}s", lambda: None
            ) as display:
                assert "thinking 0s" in "".join(visible.frames)
                assert "".join(visible.frames).count("\n") < 3
                display.write("first")
                await eventually(lambda: "first" in "".join(visible.frames))
                assert scrollback == []  # still on the live line
                previous = len(visible.frames)
                elapsed = 1
                await eventually(lambda: "1" in "".join(visible.frames[previous:]))
                display.write(" second\nthird")
                assert scrollback == ["first second\n"]
            assert scrollback == ["first second\n", "third"]

    asyncio.run(go())


def test_parallel_tools_finish_once_in_scrollback_with_live_status(tmp_path):
    async def go():
        visible = RecordingOutput()
        scrollback = []

        class GatedTool:
            description, parameters, read_only = "wait for release", {}, True

            def __init__(self, name):
                self.name = name
                self.release = asyncio.Event()

            async def run(self, args, ctx):
                await self.release.wait()
                return ToolResult("done")

        async def read(prompt):
            raise EOFError

        first, second = GatedTool("slow_a"), GatedTool("slow_b")
        provider = ScriptedProvider(
            [Turn(calls=[(first.name, {}), (second.name, {})]), Turn(text="Answer.")]
        )
        app = ChatApp(
            config=SessionConfig(
                model="test", workspace=tmp_path, home=tmp_path / "home", stream_deltas=True
            ),
            provider=provider,
            read_line=read,
            write=scrollback.append,
            extra_tools=[first, second],
            display_factory=TurnDisplay,
        )
        with create_pipe_input() as keys, create_app_session(input=keys, output=visible):
            task = asyncio.create_task(app.turn("run both"))
            try:
                await eventually(lambda: "Running slow_a, slow_b" in "".join(visible.frames))
                assert not scrollback
                second.release.set()
                await eventually(lambda: len(scrollback) == 1)
                assert scrollback[0].startswith("● slow_b {}  ✓ done · ")
                assert "Running slow_a" in app._progress()
                first.release.set()
                assert await asyncio.wait_for(task, 2) == "completed"
            finally:
                if not task.done():
                    task.cancel()
                    await task
        lines = "".join(scrollback).splitlines()
        assert lines[1].startswith("● slow_a {}  ✓ done · ")
        assert lines[2:4] == ["", "Answer."]
        assert sum(line.startswith("● ") for line in lines) == 2
        assert not any("Running slow_" in line for line in lines)

    asyncio.run(go())


def test_resize_and_narrow_terminal_preserve_a_long_unicode_line():
    out = RecordingOutput()
    scrollback = []
    line = "界кириллица" * 5_000

    async def go():
        with create_pipe_input() as keys, create_app_session(input=keys, output=out):
            async with TurnDisplay(scrollback.append, lambda: "0.0s working", lambda: None) as view:
                view.write(line)
                for size in [Size(rows=2, columns=5), Size(rows=12, columns=26)]:
                    out.size = size
                    await eventually(lambda size=size: size in out.rendered_sizes)
                view.write(" done\n")
        assert scrollback == [line + " done\n"]

    asyncio.run(go())


@pytest.mark.parametrize("key", [b"\x1b", b"\x03"], ids=["escape", "ctrl-c"])
def test_run_keys_stop_and_wait_for_tool_cleanup(tmp_path, key):
    async def go():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowTool:
            name, description, parameters, read_only = "wait", "wait for stop", {}, True

            async def run(self, args, ctx):
                started.set()
                try:
                    await asyncio.sleep(3600)
                finally:
                    await asyncio.sleep(0.02)
                    cleaned.set()

        async def read(prompt):
            raise EOFError

        provider = ScriptedProvider([Turn(calls=[("wait", {})]), Turn(text="after")])
        out = []
        app = ChatApp(
            config=SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home"),
            provider=provider,
            read_line=read,
            write=out.append,
            extra_tools=[SlowTool()],
            display_factory=TurnDisplay,
        )
        session_id = app.session.session_id
        with create_pipe_input() as keys, create_app_session(input=keys, output=RecordingOutput()):
            running = asyncio.create_task(app.turn("start"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                keys.send_bytes(key * 2)
                assert await asyncio.wait_for(asyncio.shield(running), 2) == "cancelled"
                assert cleaned.is_set()
            finally:
                if not running.done():
                    running.cancel()
                    await running
            assert await app.turn("again") == "completed"
        assert app.session.session_id == session_id
        assert "".join(out).count("! stopped") == 1
        assert "after" in "".join(out)

    asyncio.run(go())


def test_prompt_cancels_draft_and_exits_after_two_empty_interrupts(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    history = tmp_path / "new" / "history"
    visible = RecordingOutput()
    out = []

    async def go():
        with create_pipe_input() as keys, create_app_session(input=keys, output=visible):
            read, answer = terminal_io(history)
            prompts = asyncio.Queue()

            async def read_line(prompt):
                prompts.put_nowait(prompt)
                return await read(prompt)

            provider = ScriptedProvider([Turn(text="ответ")])
            app = ChatApp(
                config=SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home"),
                provider=provider,
                read_line=read_line,
                read_answer=answer,
                write=out.append,
            )
            running = asyncio.create_task(app.run())
            async with asyncio.timeout(4):
                await prompts.get()
                assert not history.parent.exists()
                keys.send_text("черновик\x03")
                await prompts.get()
                keys.send_text("привет\r")
                await prompts.get()
                keys.send_bytes(b"\x03")
                await prompts.get()
                keys.send_bytes(b"\x03")
                assert await running == 0
            assert provider.requests[0].messages[0].content == "привет"

    asyncio.run(go())
    assert "".join(out).count("Press Ctrl+C again to exit") == 1
    assert "привет" in history.read_text(encoding="utf-8")


def test_escape_during_approval_cancels_parallel_tool_and_reopens_prompt(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    visible = RecordingOutput()
    out = []

    async def go():
        cleaned = asyncio.Event()
        next_prompt = asyncio.Event()
        prompts = 0

        class Wait:
            name, description, parameters, read_only = "wait", "wait for stop", {}, True

            async def run(self, args, ctx):
                try:
                    await asyncio.sleep(3600)
                finally:
                    cleaned.set()

        class Ask:
            name, description, parameters, read_only = "ask", "request approval", {}, True

            async def run(self, args, ctx):
                approved = await ctx.approve(ApprovalRequest("ask", "demo", "rule", "question"))
                return ToolResult(str(approved))

        with create_pipe_input() as keys, create_app_session(input=keys, output=visible):
            read, answer = terminal_io(None)

            async def read_line(prompt):
                nonlocal prompts
                prompts += 1
                if prompts == 2:
                    assert cleaned.is_set()
                    next_prompt.set()
                return "task" if prompts == 1 else await read(prompt)

            app = ChatApp(
                config=SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home"),
                provider=ScriptedProvider([Turn(calls=[("wait", {}), ("ask", {})])]),
                read_line=read_line,
                read_answer=answer,
                write=out.append,
                extra_tools=[Wait(), Ask()],
                display_factory=TurnDisplay,
            )
            running = asyncio.create_task(app.run())
            async with asyncio.timeout(5):
                await eventually(lambda: "Run it?" in "".join(visible.frames))
                keys.send_bytes(b"\x1b")
                await next_prompt.wait()
                keys.send_bytes(b"\x03")
                assert await running == 0
        assert prompts == 2

    asyncio.run(go())
    assert "".join(out).count("! stopped") == 1
