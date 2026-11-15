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
                await tool(server, "karox_status", run_id=run_id, since=since, wait_seconds=2)
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
        assert tail["structuredContent"]["events"] == []
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
        await asyncio.sleep(0.2)  # let the killed shell's pipes close before the loop does

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
            evil = {"Mcp-Session-Id": sid, "Origin": "https://evil.example"}
            assert (await client.post(f"/mcp/{TOKEN}", json=ping, headers=evil)).status_code == 403
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
