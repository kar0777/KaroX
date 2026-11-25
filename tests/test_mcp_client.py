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


def test_http_event_stream_replies_and_errors(ctx):
    import httpx

    from karox6.mcp.client import HttpConnection, McpServerHandle, McpTool

    seen = []

    def sse(payload):
        body = "event: message\ndata: " + json.dumps(payload) + "\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    def handler(request):
        seen.append((request.method, request.headers.get("mcp-session-id")))
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        rid, method = message.get("id"), message["method"]
        if method == "initialize":
            reply = sse({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "x"}})
            reply.headers["mcp-session-id"] = "sess-1"
            return reply
        if method == "tools/call" and message["params"]["name"] == "fails":
            return sse({"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "nope"}})
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": "bad input"}], "isError": True}
            # a progress notification first, then the reply
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            body = "data: " + json.dumps(note) + "\n\n"
            body += "data: " + json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return httpx.Response(202)

    async def go():
        conn = HttpConnection("http://mcp.test/mcp", transport=httpx.MockTransport(handler))
        await conn.request("initialize", {}, 10)
        assert conn.session_id == "sess-1"
        handle = McpServerHandle("remote", conn, [])
        tool = McpTool(handle, {"name": "check", "inputSchema": {"type": "object"}})
        failing = McpTool(handle, {"name": "fails", "inputSchema": {"type": "object"}})
        result = await tool.run({}, ctx)
        assert result.is_error
        assert result.content == "bad input"
        error = await failing.run({}, ctx)
        assert error.is_error
        assert "nope" in error.content
        await conn.close()

    asyncio.run(go())
    assert seen[0] == ("POST", None)
    assert seen[1] == ("POST", "sess-1")
    assert seen[-1] == ("DELETE", "sess-1")


def test_stdio_pings_cancels_and_restarts(tmp_path, ctx):
    from karox6.mcp.client import McpTool

    config = write_config(
        tmp_path, {"fake": {"command": sys.executable, "args": [FAKE], "timeout": 0.5}}
    )

    async def go():
        tools, handles = await load_mcp_tools(config)
        handle = handles[0]
        echo = next(t for t in tools if t.name == "mcp__fake__echo")

        def hidden(name):
            return McpTool(handle, {"name": name, "inputSchema": {"type": "object"}})

        try:
            assert handle.call_timeout == 0.5
            pinged = await hidden("ping_me").run({}, ctx)
            first, second = json.loads(pinged.content)
            assert first == {"jsonrpc": "2.0", "id": "srv-1", "result": {}}
            assert second["id"] == "srv-2"
            assert second["error"]["code"] == -32601

            slow = await hidden("slow").run({"seconds": 1.5}, ctx)
            assert slow.is_error
            assert "did not answer within 0.5s" in slow.content
            handle.call_timeout = 10
            cancelled = await hidden("cancelled").run({}, ctx)
            assert len(json.loads(cancelled.content)) == 1  # the server heard the cancel

            crashed = await hidden("crash").run({}, ctx)
            assert crashed.is_error
            assert "fake crashed on purpose" in crashed.content
            echoed = await echo.run({"text": "back"}, ctx)  # restarted on demand
            assert echoed.content == "echo: back"
            assert handle.restarts == 1
        finally:
            await close_all(handles)

    asyncio.run(go())


def test_failed_server_warning_quotes_its_stderr(tmp_path, capsys):
    code = "import sys; sys.stderr.write('boom: config file missing\\n'); sys.exit(3)"
    config = write_config(tmp_path, {"bad": {"command": sys.executable, "args": ["-c", code]}})
    tools, handles = asyncio.run(load_mcp_tools(config))
    assert tools == []
    assert handles == []
    err = capsys.readouterr().err
    assert "'bad' skipped" in err
    assert "boom: config file missing" in err


def test_http_starts_a_new_session_after_404(ctx):
    import httpx

    from karox6.mcp.client import HttpConnection, McpServerHandle, McpTool

    seen = []
    state = {"session": 0, "expire": False}

    def handler(request):
        sid = request.headers.get("mcp-session-id")
        seen.append((request.method, sid, request.headers.get("mcp-protocol-version")))
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            state["session"] += 1
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            headers = {"mcp-session-id": f"s{state['session']}"}
            return httpx.Response(200, json=reply, headers=headers)
        if "id" not in message:
            return httpx.Response(202)
        if state["expire"] or sid != f"s{state['session']}":
            state["expire"] = False
            return httpx.Response(404, text="unknown session")
        text = f"ok {sid}"
        result = {"content": [{"type": "text", "text": text}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def go():
        conn = HttpConnection("http://mcp.test/mcp", transport=httpx.MockTransport(handler))
        await conn.request("initialize", {"protocolVersion": "x"}, 10)
        tool = McpTool(McpServerHandle("remote", conn, []), {"name": "t"})
        assert (await tool.run({}, ctx)).content == "ok s1"
        state["expire"] = True  # the server restarted and forgot s1
        assert (await tool.run({}, ctx)).content == "ok s2"
        assert conn.sessions_started == 2
        await conn.close()

    asyncio.run(go())
    assert seen[0] == ("POST", None, None)  # the first initialize carries no session headers
    versions = [version for _, _, version in seen]
    assert versions.count(None) == 2  # only the two initialize requests go without it
    assert set(versions) == {None, "2025-06-18"}
    assert [s for m, s, _ in seen if m == "DELETE"] == ["s2"]


def test_http_concurrent_404s_renew_the_session_once(ctx):
    import httpx

    from karox6.mcp.client import HttpConnection, McpServerHandle, McpTool

    state = {"session": 0, "dead": set()}

    def handler(request):
        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            assert sid is None  # a new session never reuses the old id
            state["session"] += 1
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            return httpx.Response(
                200, json=reply, headers={"mcp-session-id": f"s{state['session']}"}
            )
        if "id" not in message:
            return httpx.Response(202)
        if sid in state["dead"]:
            return httpx.Response(404)
        # a server request that reuses the client's id must not be taken for the reply
        ping = {"jsonrpc": "2.0", "id": message["id"], "method": "ping"}
        reply = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": sid}]},
        }
        body = f"data: {json.dumps(ping)}\n\ndata: {json.dumps(reply)}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async def go():
        conn = HttpConnection("http://mcp.test/mcp", transport=httpx.MockTransport(handler))
        await conn.request("initialize", {"protocolVersion": "x"}, 10)
        tool = McpTool(McpServerHandle("remote", conn, []), {"name": "t"})
        state["dead"].add("s1")
        results = await asyncio.gather(*(tool.run({}, ctx) for _ in range(4)))
        assert [r.content for r in results] == ["s2"] * 4
        assert conn.sessions_started == 2
        await conn.close()

    asyncio.run(go())


def test_stdio_close_leaves_no_process(tmp_path):
    from karox6.mcp.client import connect

    async def go():
        handle = await connect("fake", {"command": sys.executable, "args": [FAKE]})
        proc = handle.connection.proc
        await handle.connection.close()
        assert proc.returncode is not None
        assert proc.stdin.is_closing()

    asyncio.run(go())


def test_cancelled_load_closes_the_servers_already_connected(tmp_path, monkeypatch):
    import contextlib

    import pytest

    from karox6.mcp import client

    config = write_config(
        tmp_path,
        {
            "fake": {"command": sys.executable, "args": [FAKE]},
            # Never answers initialize: the load is still waiting on it when cancelled.
            "mute": {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
        },
    )
    closed: list[str] = []
    real_close = client.close_all

    async def recording_close(handles):
        closed.extend(h.name for h in handles)
        await real_close(handles)

    monkeypatch.setattr(client, "close_all", recording_close)

    async def go():
        task = asyncio.ensure_future(load_mcp_tools(config))
        await asyncio.sleep(3)  # the fake server connects in well under a second
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert closed == ["fake"]

    bad = tmp_path / "list.json"
    bad.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        asyncio.run(load_mcp_tools(bad))


def test_stdio_command_is_resolved_through_pathext_on_windows(monkeypatch):
    from karox6.mcp import client

    monkeypatch.setattr(client.sys, "platform", "win32")
    monkeypatch.setattr(
        client.shutil, "which", lambda name: "C:/node/npx.CMD" if name == "npx" else None
    )
    assert client._executable("npx") == "C:/node/npx.CMD"
    assert client._executable("no-such-tool") == "no-such-tool"
    monkeypatch.setattr(client.sys, "platform", "linux")
    assert client._executable("npx") == "npx"
