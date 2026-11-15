"""Code mode: run_code programs call the session's tools through a loopback socket."""

import asyncio
import sys
import textwrap

from karox6.core.events import RunFinished
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools import code as code_module
from karox6.tools.base import ToolResult
from karox6.tools.code import RunCodeTool
from karox6.tools.fs import EditTool, ReadTool
from karox6.tools.search import GrepTool


def program(source):
    return textwrap.dedent(source).strip() + "\n"


class Collect:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def run_code(ctx, source, tools=None, **args):
    tool_list = tools or [ReadTool(), EditTool(), GrepTool()]
    tool_map = {t.name: t for t in tool_list}
    tool = RunCodeTool(lambda: tool_map)
    sink = Collect()
    ctx.emit = sink
    result = asyncio.run(tool.run({"code": program(source), **args}, ctx))
    return result, sink.events


def test_program_calls_tools_and_prints(ctx, monkeypatch):
    monkeypatch.setattr(code_module, "find_python", lambda env: sys.executable)
    (ctx.workspace / "a.py").write_text("x = 1\ny = 2\n")
    (ctx.workspace / "b.py").write_text("z = 3\n")
    result, events = run_code(
        ctx,
        """
        for name in ("a.py", "b.py"):
            text = tools.read(file_path=name)
            print(name, len(text.splitlines()))
        print(tools.edit("a.py", "y = 2", "y = 20"))
        """,
    )
    assert not result.is_error, result.content
    assert "a.py" in result.content
    assert "b.py" in result.content
    assert "Edited a.py" in result.content
    assert "[exit code: 0; 3 tool calls" in result.content
    assert (ctx.workspace / "a.py").read_text() == "x = 1\ny = 20\n"
    names = [e.name for e in events if e.type == "tool_finished"]
    assert names == ["run_code/read", "run_code/read", "run_code/edit"]


def test_failed_call_raises_and_fails_the_program(ctx, monkeypatch):
    monkeypatch.setattr(code_module, "find_python", lambda env: sys.executable)
    caught, _ = run_code(
        ctx,
        """
        try:
            tools.read(file_path="missing.py")
        except ToolError as error:
            print("caught:", error)
        print("after")
        """,
    )
    assert not caught.is_error
    assert "caught:" in caught.content
    assert "after" in caught.content
    failed, _ = run_code(ctx, 'tools.read(file_path="missing.py")\nprint("unreached")')
    assert failed.is_error
    assert "ToolError" in failed.content
    assert "unreached" not in failed.content
    assert "[exit code: 1" in failed.content


def test_unknown_tool_bad_arguments_and_no_nesting(ctx, monkeypatch):
    monkeypatch.setattr(code_module, "find_python", lambda env: sys.executable)
    result, _ = run_code(
        ctx,
        """
        for call in (lambda: tools.nope(), lambda: tools.read(limit=3),
                     lambda: tools.run_code(code="1"), lambda: tools.read("a", 1, 2, 3, 4)):
            try:
                call()
            except ToolError as error:
                print("refused:", str(error).splitlines()[0])
        """,
    )
    assert not result.is_error, result.content
    lines = [ln for ln in result.content.splitlines() if ln.startswith("refused:")]
    assert len(lines) == 4
    assert "unknown tool 'nope'" in lines[0]
    assert "file_path" in lines[1]
    assert "unknown tool 'run_code'" in lines[2]
    assert "positional" in lines[3]


def test_timeout_kills_the_program(ctx, monkeypatch):
    monkeypatch.setattr(code_module, "find_python", lambda env: sys.executable)
    result, _ = run_code(
        ctx, "import time\nprint('start', flush=True)\ntime.sleep(30)", timeout=1500
    )
    assert result.is_error
    assert "start" in result.content
    assert "timed out" in result.content


def test_wrong_token_is_ignored(ctx):
    """Another local process cannot drive the tools without the program's token."""

    class Spy:
        name = "spy"
        description = "records calls"
        parameters = {"type": "object", "properties": {}}
        read_only = True
        called = False

        async def run(self, args, ctx):
            Spy.called = True
            return ToolResult("spied")

    async def go():
        nested = code_module._NestedCalls({"spy": Spy()}, ctx, "right-token")
        server = await asyncio.start_server(nested.serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"token": "wrong"}\n{"id": 1, "tool": "spy", "args": {}}\n')
        await writer.drain()
        reply = await reader.read()
        writer.close()
        server.close()
        return reply

    assert asyncio.run(go()) == b""
    assert Spy.called is False


def test_session_offers_run_code_only_with_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(code_module, "find_python", lambda env: sys.executable)
    (tmp_path / "a.txt").write_text("hello\n")
    turns = [
        Turn(calls=[("run_code", {"code": "print(tools.read(file_path='a.txt'))"})]),
        Turn(text="done"),
    ]
    config = SessionConfig(model="t", workspace=tmp_path, home=tmp_path / "home", code_mode=True)
    session = Session(config, ScriptedProvider(turns))
    assert "run_code" in [t.name for t in session.tools]

    async def collect():
        return [event async for event in session.run("task")]

    events = asyncio.run(collect())
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "completed"
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert "hello" in tool_messages[0].content
    plain = SessionConfig(model="t", workspace=tmp_path, home=tmp_path / "home")
    assert "run_code" not in [t.name for t in Session(plain, ScriptedProvider([])).tools]
