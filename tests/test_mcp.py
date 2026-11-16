import asyncio
import json
import subprocess

import httpx
import pytest

from karox6.core.session import SessionConfig
from karox6.mcp.protocol import TOOLS, McpServer
from karox6.mcp.runs import RunManager
from karox6.mcp.transport import HttpMcp, serve_lines
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ApprovalRequest, ToolResult

TOKEN = "t" * 24


class ApprovalTool:
    """Asks for approval like a destructive shell command would, on every OS."""

    name = "danger"
    description = "needs approval"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    async def run(self, args, ctx):
        ok = await ctx.approve(ApprovalRequest("danger", "rm -rf build", "rm.test", "test rule"))
        return ToolResult("ran" if ok else "denied by user", is_error=not ok)


def make_manager(tmp_path, turns, **kwargs):
    providers = []

    def config(ws):
        return SessionConfig(model="test", workspace=ws, home=tmp_path / "home")

    def provider():
        p = ScriptedProvider(list(turns))
        providers.append(p)
        return p

    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    manager = RunManager(config, provider, root=root, **kwargs)
    return manager, providers


def call(server, method, params=None, rid=1):
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def tool(server, name, **arguments):
    async def go():
        reply = await call(server, "tools/call", {"name": name, "arguments": arguments})
        return reply["result"]

    return go()


def test_initialize_list_ping(tmp_path):
    manager, _ = make_manager(tmp_path, [])
    server = McpServer(manager)

    async def go():
        init = await call(server, "initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        old = await call(server, "initialize", {"protocolVersion": "1999-01-01"})
        assert old["result"]["protocolVersion"] == "2025-06-18"
        listed = await call(server, "tools/list")
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {t["name"] for t in TOOLS}
        assert (await call(server, "ping"))["result"] == {}
        missing = await call(server, "nope")
        assert missing["error"]["code"] == -32601
        note = await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note is None

    asyncio.run(go())


def test_run_to_completion_and_idempotency(tmp_path):
    manager, providers = make_manager(tmp_path, [Turn(text="all done")])
    server = McpServer(manager)

    async def go():
        started = await tool(server, "karox_run", task="say hi", request_id="req-1")
        data = started["structuredContent"]
        run_id = data["run_id"]
        again = await tool(server, "karox_run", task="say hi", request_id="req-1")
        assert again["structuredContent"]["run_id"] == run_id
        assert again["structuredContent"]["duplicate"] is True
        status, since, seen = {"status": "running"}, 0, []
        for _ in range(50):
            status = (
                await tool(
                    server,
                    "karox_status",
                    run_id=run_id,
                    since=since,
                    wait_seconds=2,
                    detail="full",
                )
            )["structuredContent"]
            seen += status["events"]
            since = status["next"]
            if status["status"] != "running":
                break
        assert status["status"] == "completed"
        assert status["final_text"] == "all done"
        assert seen[0]["type"] == "run_started"
        assert seen[-1]["type"] == "run_finished"
        assert [e["i"] for e in seen] == list(range(len(seen)))
        tail = await tool(server, "karox_status", run_id=run_id, since=status["next"])
        assert tail["structuredContent"]["log"] == []
        log = (await tool(server, "karox_status", run_id=run_id))["structuredContent"]["log"]
        assert log[0].startswith("started (session ")
        assert log[-2:] == ["says: all done", "finished: completed"]
        assert (
            "events" not in (await tool(server, "karox_status", run_id=run_id))["structuredContent"]
        )
        bad = await tool(server, "karox_status", run_id=run_id, detail="everything")
        assert bad["isError"] is True
        runs = (await tool(server, "karox_runs"))["structuredContent"]["runs"]
        assert [r["run_id"] for r in runs] == [run_id]
        await manager.shutdown()

    asyncio.run(go())
    assert len(providers) == 1  # the duplicate call never built a second session


def test_approval_flow(tmp_path):
    turns = [Turn(calls=[("danger", {})]), Turn(text="finished")]
    providers = []

    def config(ws):
        return SessionConfig(model="test", workspace=ws, home=tmp_path / "home")

    def provider():
        providers.append(ScriptedProvider(list(turns)))
        return providers[-1]

    root = tmp_path / "root"
    root.mkdir()
    manager = RunManager(config, provider, root=root)
    server = McpServer(manager)

    async def go():
        # inject the approval-asking tool into the session the manager builds
        run, _ = manager.start("do it")
        run.session.tools.append(ApprovalTool())
        run.session._tool_map["danger"] = run.session.tools[-1]
        pending, since = [], 0
        for _ in range(50):
            status = await manager.status(run.run_id, since=since, wait_s=1)
            since = status["next"]
            pending = status["pending_approvals"]
            if pending:
                break
        assert pending
        assert pending[0]["command"] == "rm -rf build"
        answer = await tool(
            server,
            "karox_approve",
            run_id=run.run_id,
            approval_id=pending[0]["approval_id"],
            approve=False,
        )
        assert answer["structuredContent"]["resolved"] is True
        for _ in range(50):
            status = await manager.status(run.run_id, since=since, wait_s=1)
            since = status["next"]
            if status["status"] != "running":
                break
        assert status["status"] == "completed"
        tool_results = [m for m in run.session.messages if m.role == "tool"]
        assert "denied" in tool_results[0].content
        stale = manager.approve(run.run_id, pending[0]["approval_id"], True)
        assert stale["resolved"] is False

    asyncio.run(go())


def test_errors_and_workspace_guard(tmp_path):
    manager, _ = make_manager(tmp_path, [Turn(text="x")])
    server = McpServer(manager)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    async def go():
        bad = await tool(server, "karox_run", task="t", cwd=str(outside))
        assert bad["isError"] is True
        assert "outside the server root" in bad["content"][0]["text"]
        unknown = await tool(server, "karox_status", run_id="run-404")
        assert unknown["isError"] is True
        missing_arg = await tool(server, "karox_status")
        assert missing_arg["isError"] is True
        reply = await call(server, "tools/call", {"name": "karox_nope", "arguments": {}})
        assert reply["error"]["code"] == -32602

    asyncio.run(go())


def test_cancel_and_diff(tmp_path):
    manager, _ = make_manager(
        tmp_path, [Turn(calls=[("bash", {"command": "sleep 30", "timeout": 60000})])]
    )
    server = McpServer(manager)
    root = manager.root
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (root / "a.txt").write_text("one\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (root / "a.txt").write_text("two\n")
    (root / "new.txt").write_text("new\n")

    async def go():
        run, _ = manager.start("long")
        await asyncio.sleep(0.2)
        cancelled = (await tool(server, "karox_cancel", run_id=run.run_id))["structuredContent"]
        assert cancelled["status"] == "cancelled"
        diff = (await tool(server, "karox_diff", run_id=run.run_id))["structuredContent"]
        assert diff["git"] is True
        assert "+two" in diff["diff"]
        assert "new.txt" in diff["untracked"]
        files = (await tool(server, "karox_files", run_id=run.run_id))["structuredContent"]
        assert files["files"] == []  # only files this session wrote count
        await manager.shutdown()

    asyncio.run(go())


def test_busy_workspace_is_refused(tmp_path):
    manager, _ = make_manager(
        tmp_path, [Turn(calls=[("bash", {"command": "sleep 30", "timeout": 60000})])]
    )

    async def go():
        manager.start("first")
        with pytest.raises(Exception, match="still working"):
            manager.start("second")
        await manager.shutdown()

    asyncio.run(go())


def test_client_cancels_a_long_poll(tmp_path):
    """notifications/cancelled ends that client's request (no answer); the run goes on."""
    manager, _ = make_manager(
        tmp_path, [Turn(calls=[("bash", {"command": "sleep 30", "timeout": 60000})])]
    )
    server = McpServer(manager)
    out = []

    async def write(payload):
        out.append(payload)

    def message(rid, **arguments):
        params = {"name": "karox_status", "arguments": arguments}
        return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}

    def cancel(rid):
        params = {"requestId": rid, "reason": "user pressed Esc"}
        return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}

    async def go():
        run, _ = manager.start("long")
        poll = asyncio.create_task(
            server.handle(message(7, run_id=run.run_id, wait_seconds=30), peer="a")
        )
        await asyncio.sleep(0.2)
        assert await server.handle(cancel(7), peer="b") is None  # another client's ids
        assert await server.handle(cancel("7"), peer="a") is None  # a different id
        await asyncio.sleep(0.2)
        assert not poll.done()
        await server.handle(cancel(7), peer="a")
        assert await asyncio.wait_for(poll, 2) is None

        async def lines():
            yield json.dumps(message("p", run_id=run.run_id, wait_seconds=30)).encode()
            await asyncio.sleep(0.2)
            yield json.dumps(cancel("p")).encode()
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode()

        await asyncio.wait_for(serve_lines(server, lines(), write), 5)
        assert run.running
        await manager.shutdown()

    asyncio.run(go())
    assert out == [{"jsonrpc": "2.0", "id": 2, "result": {}}]
    assert server._in_flight == {}


def test_a_cancelled_karox_cancel_gets_no_answer(tmp_path):
    """karox_cancel waits for the run to stop; that wait must not swallow the client's
    cancellation of the karox_cancel request itself."""
    manager, _ = make_manager(
        tmp_path, [Turn(calls=[("bash", {"command": "sleep 30", "timeout": 60000})])]
    )
    server = McpServer(manager)

    async def go():
        run, _ = manager.start("long")
        await asyncio.sleep(0.3)  # the command is running
        params = {"name": "karox_cancel", "arguments": {"run_id": run.run_id}}
        request = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params}
        pending = asyncio.create_task(server.handle(request))
        for _ in range(3):
            await asyncio.sleep(0)
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled"}
        await server.handle({**cancel, "params": {"requestId": 9}})
        reply = await asyncio.wait_for(pending, 10)
        await manager.shutdown()
        return reply

    assert asyncio.run(go()) is None


def test_stdio_without_stdin_fails_clearly(tmp_path, monkeypatch):
    from karox6.mcp.transport import serve_stdio

    manager, _ = make_manager(tmp_path, [])
    monkeypatch.setattr("sys.stdin", None)
    with pytest.raises(OSError, match="stdin is not readable"):
        asyncio.run(serve_stdio(McpServer(manager)))


def test_stdio_lines(tmp_path):
    manager, _ = make_manager(tmp_path, [])
    server = McpServer(manager)
    out = []

    async def write(payload):
        out.append(payload)

    async def lines():
        yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        yield b"{not json"
        yield json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        yield json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"}]).encode()

    asyncio.run(serve_lines(server, lines(), write))
    assert {"jsonrpc": "2.0", "id": 1, "result": {}} in out
    assert any(isinstance(o, dict) and o.get("error", {}).get("code") == -32700 for o in out)
    assert [{"jsonrpc": "2.0", "id": 2, "result": {}}] in out
    assert len(out) == 3


def test_http_transport(tmp_path):
    manager, _ = make_manager(tmp_path, [])
    http = HttpMcp(McpServer(manager), token=TOKEN)

    async def go():
        listener = await http.start("127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        accept = {"Accept": "application/json, text/event-stream"}
        async with listener, httpx.AsyncClient(base_url=base) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.post("/mcp", json=init)).status_code == 401
            assert (await client.post("/mcp/wrong-token-xxxxxxxx", json=init)).status_code == 401
            assert (await client.post("/other", json=init)).status_code == 404
            r = await client.post(f"/mcp/{TOKEN}", json=init, headers=accept)
            assert r.status_code == 200
            sid = r.headers["mcp-session-id"]
            assert r.json()["result"]["serverInfo"]["name"] == "karox"
            assert (await client.post(f"/mcp/{TOKEN}", json=ping)).status_code == 400
            bad = await client.post(f"/mcp/{TOKEN}", json=ping, headers={"Mcp-Session-Id": "x"})
            assert bad.status_code == 404
            ok = await client.post(f"/mcp/{TOKEN}", json=ping, headers={"Mcp-Session-Id": sid})
            assert ok.status_code == 200
            assert ok.json()["result"] == {}
            bearer = {"Mcp-Session-Id": sid, "Authorization": f"Bearer {TOKEN}"}
            assert (await client.post("/mcp", json=ping, headers=bearer)).status_code == 200
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            r = await client.post(f"/mcp/{TOKEN}", json=note, headers={"Mcp-Session-Id": sid})
            assert r.status_code == 202
            assert r.content == b""
            assert (await client.get(f"/mcp/{TOKEN}")).status_code == 405
            # A page that does not know the token is refused before the token check; a
            # client that has the token works from any host (Claude, Le Chat, a web IDE).
            evil = {"Mcp-Session-Id": sid, "Origin": "https://evil.example"}
            assert (await client.post("/mcp", json=ping, headers=evil)).status_code == 403
            assert (await client.post("/mcp/wrong-token-xxxxxxxx", headers=evil)).status_code == 403
            other = {"Mcp-Session-Id": sid, "Origin": "https://claude.ai"}
            r = await client.post(f"/mcp/{TOKEN}", json=ping, headers=other)
            assert r.status_code == 200
            assert r.headers["access-control-allow-origin"] == "https://claude.ai"
            r = await client.post("/mcp", json=ping, headers={**other, **bearer})
            assert r.status_code == 200
            preflight = {
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            }
            r = await client.options("/mcp", headers=preflight)
            assert r.status_code == 204
            assert r.headers["access-control-allow-origin"] == "https://claude.ai"
            good = {"Mcp-Session-Id": sid, "Origin": "https://chatgpt.com"}
            r = await client.post(f"/mcp/{TOKEN}", json=ping, headers=good)
            assert r.status_code == 200
            assert r.headers["access-control-allow-origin"] == "https://chatgpt.com"
            assert (
                await client.request("DELETE", f"/mcp/{TOKEN}", headers={"Mcp-Session-Id": sid})
            ).status_code == 200
            gone = await client.post(f"/mcp/{TOKEN}", json=ping, headers={"Mcp-Session-Id": sid})
            assert gone.status_code == 404
            garbage = await client.post(
                f"/mcp/{TOKEN}", content=b"{oops", headers={"Mcp-Session-Id": sid}
            )
            assert garbage.status_code == 400

    asyncio.run(go())


def test_short_token_refused(tmp_path):
    manager, _ = make_manager(tmp_path, [])
    with pytest.raises(ValueError, match="at least 16"):
        HttpMcp(McpServer(manager), token="short")


async def exchange(port, data):
    """Send raw bytes, read until the server closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()


def test_http_hardening(tmp_path):
    manager, _ = make_manager(tmp_path, [])
    http = HttpMcp(McpServer(manager), token=TOKEN)
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()

    async def go():
        listener = await http.start("127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener:
            # A large upload without the token is refused from its headers alone.
            big = b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: 4194304\r\n\r\n"
            assert (await exchange(port, big)).startswith(b"HTTP/1.1 401")
            for framing in (
                "Content-Length: 5\r\nContent-Length: 0\r\n",
                "Content-Length: 5\r\nTransfer-Encoding: chunked\r\n",
                "Transfer-Encoding: gzip, chunked\r\n",
                "Content-Length: -1\r\n",
                "Content-Length : 5\r\n",
                "X: y\r\n folded\r\n",
            ):
                bad = f"POST /mcp/{TOKEN} HTTP/1.1\r\nHost: x\r\n{framing}\r\nhello".encode()
                assert (await exchange(port, bad)).startswith(b"HTTP/1.1 400"), framing
            # Chunked body with trailer fields, then the next request on the same connection.
            chunked = (
                f"POST /mcp/{TOKEN} HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                f"{len(init):x}\r\n".encode()
                + init
                + b"\r\n0\r\nX-Trailer: 1\r\n\r\n"
                + b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            out = await exchange(port, chunked)
            assert out.count(b"HTTP/1.1 200") == 2
            assert b'"serverInfo"' in out
            assert out.endswith(b"ok\n")
            # Browsers send CORS preflights without credentials.
            preflight = (
                b"OPTIONS /mcp HTTP/1.1\r\nHost: x\r\nOrigin: https://chatgpt.com\r\n"
                b"Access-Control-Request-Method: POST\r\nConnection: close\r\n\r\n"
            )
            out = await exchange(port, preflight)
            assert out.startswith(b"HTTP/1.1 204")
            assert b"Access-Control-Allow-Origin: https://chatgpt.com" in out

    asyncio.run(go())


def test_http_body_limits_and_session_cap(tmp_path, monkeypatch):
    from karox6.mcp import transport

    manager, _ = make_manager(tmp_path, [])
    http = HttpMcp(McpServer(manager), token=TOKEN)
    target = f"/mcp/{TOKEN}"
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    monkeypatch.setattr(transport, "MAX_SESSIONS", 2)

    async def go():
        deep = await http.respond("POST", target, {}, b"[" * 100_000 + b"]" * 100_000)
        assert deep[0] == 400
        empty = await http.respond("POST", target, {}, b"[]")
        assert empty[0] == 400
        sids = []
        for _ in range(3):
            status, headers, _ = await http.respond("POST", target, {}, init)
            assert status == 200
            sids.append(headers["Mcp-Session-Id"])
        assert list(http.sessions) == sids[1:]

    asyncio.run(go())


def test_run_guards(tmp_path, monkeypatch):
    from karox6.mcp import runs as runs_module

    monkeypatch.setattr(runs_module, "MAX_RUNS", 2)
    manager, _ = make_manager(tmp_path, [Turn(text="ok")])
    server = McpServer(manager)

    async def finish(run_id):
        since = 0
        for _ in range(50):
            view = await manager.status(run_id, since=since, wait_s=1)
            since = view["next"]
            if view["status"] != "running":
                return view
        raise AssertionError("run did not finish")

    async def go():
        for bad in ({"detail": "loud"}, {"wait_seconds": "soon"}):
            refused = await tool(server, "karox_run", task="zero", **bad)
            assert refused["isError"] is True
        assert manager.list() == []  # a refused call starts nothing
        first = (await tool(server, "karox_run", task="one", request_id="r1"))["structuredContent"]
        reused = await tool(server, "karox_run", task="two", request_id="r1")
        assert reused["isError"] is True
        assert "another task" in reused["content"][0]["text"]
        nan = await tool(server, "karox_status", run_id=first["run_id"], wait_seconds="nan")
        assert "isError" not in nan
        huge = await tool(server, "karox_status", run_id=first["run_id"], since=float("inf"))
        assert huge["isError"] is True
        await finish(first["run_id"])
        travel = await tool(server, "karox_run", task="t", session_id="../../outside")
        assert travel["isError"] is True
        assert "no session" in travel["content"][0]["text"]
        for task in ("two", "three"):
            run, _ = manager.start(task)
            await finish(run.run_id)
        assert first["run_id"] not in manager.runs  # the oldest finished run was forgotten
        assert "r1" not in manager.by_request
        assert len(manager.runs) == 2
        await manager.shutdown()

    asyncio.run(go())


def test_cli_drops_api_key_from_environment(monkeypatch):
    import os

    from karox6 import cli

    monkeypatch.setenv("TEST_LLM_KEY", "test-secret-value")
    args = cli._parser().parse_args(
        ["run", "--model", "m", "--api-key-env", "TEST_LLM_KEY", "--base-url", "http://x/v1", "t"]
    )
    provider = cli._provider(args)
    assert "TEST_LLM_KEY" not in os.environ  # agent shell commands inherit os.environ
    asyncio.run(provider.aclose())


def test_brief_log_lines():
    from karox6.core import events as ev
    from karox6.core.digest import event_line

    calls = {}
    assert (
        event_line(
            ev.ToolStarted(call_id="1", name="bash", arguments='{"command": "pytest -q"}'), calls
        )
        is None
    )
    line = event_line(
        ev.ToolFinished(
            call_id="1", name="bash", is_error=False, output="..\n2 passed\n", duration_ms=1500
        ),
        calls,
    )
    assert line == "✓ bash pytest -q → 2 passed (1.5s)"
    assert calls == {}
    failed = event_line(
        ev.ToolFinished(
            call_id="9", name="read", is_error=True, output="no such file", duration_ms=3
        ),
        calls,
    )
    assert failed == "✗ read → no such file"
    assert event_line(ev.AssistantMessage(step=1, text="", tool_calls=[{}]), calls) is None
    assert event_line(ev.Notice(kind="wrap_up", text="..."), calls) == "note to the agent: wrap_up"


class NapTool:
    name = "nap"
    description = "sleeps a little"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def run(self, args, ctx):
        await asyncio.sleep(0.3)
        return ToolResult("rested")


def test_brief_status_batches_until_the_run_ends(tmp_path):
    turns = [Turn(calls=[("nap", {})]), Turn(calls=[("nap", {})]), Turn(text="done")]
    manager, _ = make_manager(tmp_path, turns, extra_tools=[NapTool()])

    async def go():
        run, _ = manager.start("rest twice")
        short = await manager.status(run.run_id, wait_s=0.2, detail="brief")
        assert short["status"] == "running"  # the wait ran out first
        started = asyncio.get_running_loop().time()
        view = await manager.status(run.run_id, since=short["next"], wait_s=10, detail="brief")
        assert asyncio.get_running_loop().time() - started < 5  # woke when the run ended
        assert view["status"] == "completed"
        assert [line for line in view["log"] if line.startswith("✓ nap")]
        assert view["log"][-1] == "finished: completed"
        await manager.shutdown()

    asyncio.run(go())
