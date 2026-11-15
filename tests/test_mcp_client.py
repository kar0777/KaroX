import asyncio
import json
import sys
from pathlib import Path

from karox6.core.session import Session, SessionConfig
from karox6.mcp.client import close_all, load_mcp_tools, render_result, tool_name
from karox6.mcp.protocol import McpServer
from karox6.mcp.runs import RunManager
from karox6.mcp.transport import HttpMcp
from karox6.providers.scripted import ScriptedProvider, Turn

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))
TOKEN = "k" * 24


def write_config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_stdio_server_tools_and_calls(tmp_path, ctx):
    config = write_config(
        tmp_path,
        {
            "fake": {"command": sys.executable, "args": [FAKE]},
            "broken": {"command": str(tmp_path / "no-such-binary")},
            "off": {"command": sys.executable, "args": [FAKE], "disabled": True},
        },
    )

    async def go():
        tools, handles = await load_mcp_tools(config)
        try:
            by_name = {t.name: t for t in tools}
            assert set(by_name) == {"mcp__fake__echo", "mcp__fake__add"}  # paged list, 2 pages
            assert by_name["mcp__fake__add"].read_only is True
            assert by_name["mcp__fake__echo"].read_only is False
            echoed = await by_name["mcp__fake__echo"].run({"text": "hi"}, ctx)
            assert echoed.content == "echo: hi"
            summed = await by_name["mcp__fake__add"].run({"a": 2, "b": 3}, ctx)
            assert json.loads(summed.content) == {"sum": 5}
        finally:
            await close_all(handles)

    asyncio.run(go())


def test_session_uses_mcp_tool(tmp_path):
    config = write_config(tmp_path, {"fake": {"command": sys.executable, "args": [FAKE]}})

    async def go():
        tools, handles = await load_mcp_tools(config)
        provider = ScriptedProvider(
            [Turn(calls=[("mcp__fake__echo", {"text": "from model"})]), Turn(text="ok")]
        )
        cfg = SessionConfig(model="t", workspace=tmp_path, home=tmp_path / "home")
        session = Session(cfg, provider, extra_tools=tools)
        try:
            events = [e async for e in session.run("go")]
        finally:
            await close_all(handles)
        assert events[-1].status == "completed"
        names = [s.name for s in provider.requests[0].tools]
        assert "mcp__fake__echo" in names
        assert "bash" in names or "shell" in names or "powershell" in names
        result = next(m for m in session.messages if m.role == "tool")
        assert result.content == "echo: from model"

    asyncio.run(go())


def test_http_server_with_bearer(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    manager = RunManager(
        lambda ws: SessionConfig(model="t", workspace=ws, home=tmp_path / "home"),
        lambda: ScriptedProvider([]),
        root=root,
    )
    http = HttpMcp(McpServer(manager), token=TOKEN)
    monkeypatch.setenv("TEST_MCP_TOKEN", TOKEN)

    async def go():
        listener = await http.start("127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        config = write_config(
            tmp_path,
            {
                "karox": {
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "bearer_token_env": "TEST_MCP_TOKEN",
                },
                "nokey": {"url": f"http://127.0.0.1:{port}/mcp", "bearer_token_env": "UNSET_X"},
            },
        )
        async with listener:
            tools, handles = await load_mcp_tools(config)
            try:
                by_name = {t.name: t for t in tools}
                assert "mcp__karox__karox_runs" in by_name
                assert all(n.startswith("mcp__karox__") for n in by_name)
                assert by_name["mcp__karox__karox_runs"].read_only is True
                handle = handles[0]
                assert handle.connection.session_id in http.sessions
            finally:
                await close_all(handles)
            assert http.sessions == {}  # the client ended its MCP session

    asyncio.run(go())


def test_helpers():
    assert tool_name("my server", "do.thing") == "mcp__my_server__do_thing"
    assert len(tool_name("s" * 80, "t")) == 64
    assert render_result({"content": [{"type": "image", "data": "x"}]}) == "[image content omitted]"
    assert render_result({"content": []}) == "(no output)"
    text = render_result({"content": [{"type": "resource", "resource": {"text": "body"}}]})
    assert text == "body"
