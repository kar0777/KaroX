import asyncio
import sys
import textwrap

import pytest

from karox6.bridge import Bridge, BridgeError
from karox6.core.session import SessionConfig
from karox6.providers.scripted import ScriptedProvider
from karox6.tui import ChatApp

URL = "https://box.tailnet.ts.net:8443/mcp/tok123"


def stand_in(tmp_path, body):
    """A fake `karox` whose `mcp` prints what the real server prints (args go to argv.txt)."""
    script = tmp_path / "fake_karox.py"
    script.write_text(
        "import sys, time\n"
        f"open({str(tmp_path / 'argv.txt')!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_bridge_starts_reports_the_connector_url_and_stops(tmp_path):
    bridge = Bridge(tmp_path / "home")
    bridge.launcher = stand_in(
        tmp_path,
        f"""
        print("KaroX MCP (Streamable HTTP) on http://127.0.0.1:5000/mcp", file=sys.stderr)
        print("  Connector URL (public internet, Tailscale): {URL}", file=sys.stderr, flush=True)
        time.sleep(60)
        """,
    )
    assert bridge.start(mode="direct", root=tmp_path) == URL
    assert bridge.running()
    assert bridge.where == "public internet, Tailscale"
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8").split()
    assert argv[:1] == ["mcp"]
    assert "--direct" in argv
    assert "--supervise" in argv
    assert argv[argv.index("--tunnel") + 1] == "tailscale"
    bridge.stop()
    assert not bridge.running()
    assert bridge.url is None


def test_bridge_that_fails_to_start_shows_why(tmp_path):
    bridge = Bridge(tmp_path / "home")
    bridge.launcher = stand_in(
        tmp_path,
        """
        print("karox mcp: tailscale is not installed", file=sys.stderr)
        sys.exit(2)
        """,
    )
    with pytest.raises(BridgeError, match="tailscale is not installed"):
        bridge.start(mode="agent", root=tmp_path, agent_args=["--model", "m"])
    assert not bridge.running()
    assert "--model m" in (tmp_path / "argv.txt").read_text(encoding="utf-8")


def test_chat_bridge_command_shows_the_link_and_stops_on_exit(tmp_path):
    out: list[str] = []
    lines = iter(["/bridge", "/bridge", "/quit"])

    async def read_line(_prompt):
        return next(lines)

    app = ChatApp(
        config=SessionConfig(model="m", workspace=tmp_path, home=tmp_path / "home"),
        provider=ScriptedProvider([]),
        read_line=read_line,
        write=out.append,
    )
    app.bridge.launcher = stand_in(
        tmp_path,
        f"""
        print("  Connector URL (public internet, Tailscale): {URL}", file=sys.stderr, flush=True)
        time.sleep(60)
        """,
    )
    asyncio.run(app.run())
    text = "".join(out)
    assert text.count(URL) == 2  # started once, then shown again
    assert "No authentication" in text
    assert "stopping the bridge" in text
    assert not app.bridge.running()
