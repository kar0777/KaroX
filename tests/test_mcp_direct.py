import asyncio
import base64
import subprocess

import pytest

from karox6.cli import main
from karox6.mcp.direct import DirectTools
from karox6.mcp.protocol import TOOLS, McpServer
from karox6.mcp.redact import Redactor
from karox6.tools import screen
from karox6.tools.registry import build_tools
from karox6.tools.shell import detect_shell


def make_server(tmp_path, **kwargs):
    root = tmp_path / "root"
    root.mkdir()
    direct = DirectTools(root, home=tmp_path / "home", **kwargs)
    return McpServer(direct=direct), direct


async def rpc(server, method, params=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return await server.handle(message)


async def call(server, name, **arguments):
    reply = await rpc(server, "tools/call", {"name": name, "arguments": arguments})
    result = reply["result"]
    return result["content"][0]["text"], result["isError"]


def test_tools_list_and_instructions(tmp_path):
    server, direct = make_server(tmp_path)
    shell = direct.shell_name

    async def go():
        init = await rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
        assert "direct access" in init["result"]["instructions"]
        tools = {t["name"]: t for t in (await rpc(server, "tools/list"))["result"]["tools"]}
        assert set(tools) == {
            "read",
            "write",
            "edit",
            "grep",
            "glob",
            shell,
            "job_output",
            "job_list",
            "job_kill",
            "screenshot",
        }
        for name in ("read", "grep", "glob", "job_output", "job_list", "screenshot"):
            assert tools[name]["annotations"]["readOnlyHint"] is True, name
            assert tools[name]["annotations"]["destructiveHint"] is False, name
        for name in ("write", "edit", shell, "job_kill"):
            assert tools[name]["annotations"]["destructiveHint"] is True, name
            assert tools[name]["annotations"]["readOnlyHint"] is False, name
        assert tools[shell]["inputSchema"]["properties"]["confirmed"]["type"] == "boolean"
        assert "confirmed" not in direct.tools[shell].parameters["properties"]  # agent's own
        assert "50000" in tools[shell]["inputSchema"]["properties"]["timeout"]["description"]
        assert tools["screenshot"]["annotations"]["openWorldHint"] is False
        assert "Call screenshot to see" in init["result"]["instructions"]
        refused = await rpc(server, "tools/call", {"name": "karox_run", "arguments": {}})
        assert refused["error"]["code"] == -32602
        await direct.shutdown()

    asyncio.run(go())


def test_confirmed_is_offered_only_when_asking(tmp_path):
    _, direct = make_server(tmp_path, approval_mode="deny")
    shell = next(t for t in direct.specs() if t["name"] == direct.shell_name)
    assert "confirmed" not in shell["inputSchema"]["properties"]
    assert "leave such steps to the user" in direct.instructions


def test_read_write_edit_round_trip(tmp_path):
    server, direct = make_server(tmp_path)
    root = direct.root

    async def go():
        text, error = await call(server, "write", file_path="pkg/a.txt", content="one\ntwo\n")
        assert (error, text) == (False, "Created pkg/a.txt (2 lines).")
        text, error = await call(server, "read", file_path="pkg/a.txt")
        assert not error
        assert "1: one\n2: two" in text
        text, error = await call(
            server, "edit", file_path="pkg/a.txt", old_string="two", new_string="2"
        )
        assert (error, text) == (False, "Edited pkg/a.txt: replaced 1 occurrence.")
        assert (root / "pkg" / "a.txt").read_text() == "one\n2\n"
        (root / "b.txt").write_text("old\n")
        text, error = await call(server, "write", file_path="b.txt", content="new\n")
        assert error
        assert "has not been read" in text
        text, error = await call(server, "grep", pattern="^one$")
        assert not error
        assert "pkg/a.txt" in text
        text, error = await call(server, "glob", pattern="*.txt")
        assert not error
        assert "b.txt" in text
        text, error = await call(server, "edit", file_path="pkg/a.txt")
        assert error
        assert "missing required 'old_string'" in text
        await direct.shutdown()

    asyncio.run(go())


def test_shell_and_background_job(tmp_path):
    server, direct = make_server(tmp_path)
    shell = direct.shell_name

    async def go():
        text, error = await call(server, shell, command="echo hello-direct")
        assert not error
        assert "hello-direct" in text
        assert text.endswith("[exit code: 0]")
        started, error = await call(server, shell, command="echo bg-out", run_in_background=True)
        assert not error
        assert "background job 1" in started
        text, error = await call(server, "job_output", job_id="1", wait=True, timeout=20000)
        assert not error
        assert "bg-out" in text
        assert "exited with code 0" in text
        listed, _ = await call(server, "job_list")
        assert listed.startswith("1: exited with code 0")
        await direct.shutdown()

    asyncio.run(go())


def test_concurrent_commands_keep_their_own_output(tmp_path):
    """Clients may send calls in parallel: each command needs its own log and script."""
    server, direct = make_server(tmp_path)

    async def go():
        commands = [f"echo out-{i}" for i in range(4)]
        results = await asyncio.gather(
            *(call(server, direct.shell_name, command=c) for c in commands)
        )
        for i, (text, error) in enumerate(results):
            assert not error
            assert text.startswith(f"out-{i}\n")
        await direct.shutdown()

    asyncio.run(go())


def test_a_long_command_returns_before_the_client_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr("karox6.mcp.direct.CALL_WAIT_MS", 1000)
    server, direct = make_server(tmp_path)

    async def go():
        text, error = await call(server, direct.shell_name, command="sleep 20", timeout=600000)
        assert not error
        assert "still running after 1000 ms; moved to background job 1" in text
        text, error = await call(server, "job_kill", job_id="1")
        assert (error, text) == (False, "Job 1 stopped.")
        await direct.shutdown()

    asyncio.run(go())


def git_root(tmp_path, mode):
    server, direct = make_server(tmp_path, approval_mode=mode)
    # A fresh repository without remotes: the force push below can only fail.
    subprocess.run(["git", "init", "-q", str(direct.root)], check=True)
    return server, direct


PUSH = "git push --force origin main"


def test_destructive_command_needs_confirmation_when_asking(tmp_path):
    server, direct = git_root(tmp_path, "ask")
    shell = direct.shell_name

    async def go():
        text, error = await call(server, shell, command=PUSH)
        assert error
        assert text.startswith("Not executed")
        assert "git.push-force" in text
        assert PUSH in text
        assert "confirmed: true" in text
        text, error = await call(server, shell, command=PUSH, confirmed=True)
        assert "Not executed" not in text
        assert "[exit code: " in text  # it ran (and failed: there is no remote)
        await direct.shutdown()

    asyncio.run(go())


@pytest.mark.parametrize(("mode", "runs"), [("allow", True), ("deny", False)])
def test_destructive_command_under_allow_and_deny(tmp_path, mode, runs):
    server, direct = git_root(tmp_path, mode)

    async def go():
        text, _ = await call(server, direct.shell_name, command=PUSH, confirmed=True)
        assert ("[exit code: " in text) is runs
        assert ("Not executed" in text) is not runs
        await direct.shutdown()

    asyncio.run(go())


def test_paths_outside_the_root_are_refused(tmp_path):
    server, direct = make_server(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")

    async def go():
        for name, args in (
            ("read", {"file_path": str(outside)}),
            ("read", {"file_path": "../outside.txt"}),
            ("write", {"file_path": "../new.txt", "content": "x"}),
            ("grep", {"pattern": "secret", "path": str(tmp_path)}),
            (direct.shell_name, {"command": "echo hi", "workdir": str(tmp_path)}),
        ):
            text, error = await call(server, name, **args)
            assert error, (name, args)
            assert "outside the project root" in text, text
        assert not (tmp_path / "new.txt").exists()
        await direct.shutdown()

    asyncio.run(go())


def test_any_cwd_lifts_the_root(tmp_path):
    server, direct = make_server(tmp_path, any_path=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")

    async def go():
        text, error = await call(server, "read", file_path=str(outside))
        assert not error
        assert "1: secret" in text
        await direct.shutdown()

    asyncio.run(go())


def test_direct_mode_needs_no_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAROX_HOME", str(tmp_path))  # no user config with a default model
    monkeypatch.delenv("KAROX_MODEL", raising=False)
    monkeypatch.delenv("KAROX_CONFIG", raising=False)
    with pytest.raises(SystemExit):
        main(["mcp", "--tunnel", "tailscale", "--no-mcp"])
    assert "--model is required" in capsys.readouterr().err
    # Past the model check: the next check (tunnel without --http) answers instead.
    assert main(["mcp", "--direct", "--tunnel", "tailscale", "--no-mcp"]) == 2
    assert "--tunnel needs --http" in capsys.readouterr().err


def test_screenshot_can_be_left_out_and_is_not_an_agent_tool(tmp_path):
    _, direct = make_server(tmp_path, screenshot=False)
    assert "screenshot" not in {t["name"] for t in direct.specs()}
    assert "Call screenshot" not in direct.instructions  # the root path may say screenshot
    assert "screenshot" not in {t["name"] for t in TOOLS}
    assert "screenshot" not in {t.name for t in build_tools(detect_shell())}


def test_no_screenshot_flag_reaches_direct_mode(tmp_path, monkeypatch):
    seen = {}

    class Stop(ValueError):
        pass

    def fake_direct(root, **kwargs):
        seen.update(kwargs)
        raise Stop("stop here")

    monkeypatch.setattr("karox6.mcp.direct.DirectTools", fake_direct)
    # A key variable of its own: direct mode removes it from this process's environment.
    common = ["mcp", "--direct", "--no-mcp", "--api-key-env", "KAROX_SCREEN_TEST_KEY"]
    common += ["--home", str(tmp_path), "--root", str(tmp_path)]
    assert main([*common, "--no-screenshot"]) == 2
    assert seen["screenshot"] is False
    assert main(common) == 2
    assert seen["screenshot"] is True


def test_screenshot_reply_carries_the_image(tmp_path, monkeypatch):
    server, direct = make_server(tmp_path)
    png = screen.encode_png(2, 1, bytes([255, 0, 0, 0, 0, 255]))
    asked = []

    def fake_capture(window, max_width):
        asked.append((window, max_width))
        if window == "missing":
            raise screen.ScreenError("no visible window title contains 'missing'")
        return screen.Shot(png, 2, 1, "the whole desktop (1 monitor, 2x1 px)")

    monkeypatch.setattr(screen, "capture", fake_capture)

    async def go():
        params = {"name": "screenshot", "arguments": {"max_width": 99999}}
        result = (await rpc(server, "tools/call", params))["result"]
        image, text = result["content"]
        assert image == {
            "type": "image",
            "data": base64.b64encode(png).decode("ascii"),
            "mimeType": "image/png",
        }
        assert text["type"] == "text"
        assert text["text"].startswith("Captured the whole desktop (1 monitor, 2x1 px)")
        assert result["isError"] is False
        text, error = await call(server, "screenshot", window="missing")
        assert error
        assert text == "Error: no visible window title contains 'missing'"
        assert asked == [(None, screen.MAX_WIDTH), ("missing", screen.DEFAULT_WIDTH)]
        await direct.shutdown()

    asyncio.run(go())


FAKE_KEY = "sk-proj-" + "FAKE0fake" * 5  # fake: masked as an API key
MARKED_KEY = "[secret removed: API key]"


def test_secrets_are_masked_and_markers_never_written(tmp_path):
    server, direct = make_server(tmp_path)
    shell = direct.shell_name
    env_file = direct.root / ".env"
    env_file.write_text(f"DB_PASSWORD=fake0pass0\nOPENAI_API_KEY={FAKE_KEY}\n")
    original = env_file.read_text()
    with_env = McpServer(
        direct=direct, redactor=Redactor.from_env({"MY_SERVICE_TOKEN": "Fake0value0for0tests"})
    )
    plain = McpServer(direct=direct, redactor=None)

    async def go():
        text, error = await call(server, "read", file_path=".env")
        assert not error
        assert "1: DB_PASSWORD=[secret removed: password]" in text
        assert f"2: OPENAI_API_KEY={MARKED_KEY}" in text
        text, error = await call(server, shell, command="cat .env")
        assert not error
        assert FAKE_KEY not in text
        assert "fake0pass0" not in text
        text, _ = await call(with_env, shell, command="echo Fake0value0for0tests")
        assert text.startswith("[secret removed: $MY_SERVICE_TOKEN]")
        # A marker written back would replace the real value in the file: refused.
        for name, args in (
            ("write", {"file_path": ".env", "content": f"OPENAI_API_KEY={MARKED_KEY}\n"}),
            ("edit", {"file_path": ".env", "old_string": "x", "new_string": MARKED_KEY}),
            (shell, {"command": f"echo 'KEY={MARKED_KEY}' > .env"}),
        ):
            text, error = await call(server, name, **args)
            assert error, name
            assert text.startswith("Not executed: the arguments contain"), text
        assert env_file.read_text() == original
        text, error = await call(server, "grep", pattern="secret removed")  # read-only: fine
        assert not error
        init = await rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
        assert "[secret removed: ...]" in init["result"]["instructions"]
        # Without a redactor (--no-redact) output and markers go through unchanged.
        text, _ = await call(plain, "read", file_path=".env")
        assert FAKE_KEY in text
        init = await rpc(plain, "initialize", {"protocolVersion": "2025-06-18"})
        assert "secret removed" not in init["result"]["instructions"]
        text, error = await call(plain, "write", file_path="notes.txt", content=MARKED_KEY)
        assert not error
        await direct.shutdown()

    asyncio.run(go())


def test_cli_masks_its_own_secrets(tmp_path, monkeypatch, capsys):
    """`karox mcp` masks the values of secret environment variables (taken before the key
    and token variables leave the environment) and its own access token; --no-redact
    turns masking off."""
    from karox6.mcp import transport

    servers = []

    class StopAtStart:
        def __init__(self, server, *, token, allowed_origins):
            servers.append((server, token))

        async def start(self, host, port):
            raise OSError("stopped by the test")

    monkeypatch.setattr(transport, "HttpMcp", StopAtStart)
    monkeypatch.setenv("KAROX_HOME", str(tmp_path))
    monkeypatch.delenv("KAROX_CONFIG", raising=False)
    monkeypatch.delenv("KAROX_MCP_TOKEN", raising=False)
    monkeypatch.setenv("KAROX_API_KEY", "Fake0api0key0value")
    monkeypatch.setenv("SOME_DB_PASSWORD", "Fake0db0pass0value")
    (tmp_path / "root").mkdir()
    argv = ["mcp", "--direct", "--http", "0", "--root", str(tmp_path / "root"), "--no-mcp"]
    assert main(argv) == 2
    assert "stopped by the test" in capsys.readouterr().err
    server, token = servers[-1]
    masked = server.redactor(f"a Fake0api0key0value b Fake0db0pass0value c /mcp/{token}")
    assert masked == (
        "a [secret removed: $KAROX_API_KEY] b [secret removed: $SOME_DB_PASSWORD] "
        "c /mcp/[secret removed: KaroX access token]"
    )
    monkeypatch.setenv("KAROX_API_KEY", "Fake0api0key0value")
    assert main([*argv, "--no-redact"]) == 2
    assert servers[-1][0].redactor is None
