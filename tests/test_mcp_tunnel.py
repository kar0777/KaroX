from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from karox6.cli import main
from karox6.mcp.tunnel import TIMED_OUT, TunnelError, free_port, open_tailscale

HOST = "box.tail1234.ts.net"
TARGET = "http://127.0.0.1:8765"


class FakeTailscale:
    """Answers the tailscale CLI calls open_tailscale makes and records them."""

    def __init__(
        self,
        *,
        state: str = "Running",
        dns: str = HOST + ".",
        serve: dict | None = None,
        publish: tuple[int, str] = (0, ""),
        serve_status: tuple[int, str] | None = None,
        applied: bool = True,
    ) -> None:
        self.state = state
        self.dns = dns
        self.serve = serve or {}
        self.publish = publish
        self.serve_status = serve_status
        self.applied = applied  # a publish that timed out may still have been applied
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        args = argv[1:]
        self.calls.append(args)
        if args == ["status", "--json"]:
            return 0, json.dumps({"BackendState": self.state, "Self": {"DNSName": self.dns}})
        if args == ["serve", "status", "--json"]:
            return self.serve_status or (0, json.dumps(self.serve))
        port = int(next(a for a in args if a.startswith("--https="))[8:])
        if args[-1] == "off":
            for part in ("TCP", "Web", "AllowFunnel"):
                self.serve.get(part, {}).pop(str(port), None)
                self.serve.get(part, {}).pop(f"{HOST}:{port}", None)
            return 0, ""
        if self.applied:
            route = busy(port, args[-1], funnel=args[0] == "funnel")
            self.serve = merged(self.serve, route)
        return self.publish


def busy(port: int, target: str, *, funnel: bool = True) -> dict:
    key = f"{HOST}:{port}"
    return {
        "TCP": {str(port): {"HTTPS": True}},
        "Web": {key: {"Handlers": {"/": {"Proxy": target}}}},
        "AllowFunnel": {key: True} if funnel else {},
    }


def merged(*configs: dict) -> dict:
    out: dict = {"TCP": {}, "Web": {}, "AllowFunnel": {}}
    for config in configs:
        for part, section in out.items():
            section.update(config.get(part, {}))
    return out


def test_funnel_on_the_first_free_port_and_stop_removes_only_it() -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    assert tunnel.url == f"https://{HOST}"
    assert tunnel.public
    assert fake.calls[-1] == ["funnel", "--bg", "--yes", "--https=443", TARGET]
    tunnel.stop()
    assert fake.calls[-1] == ["funnel", "--https=443", "--set-path=/", TARGET, "off"]


def test_routes_of_other_programs_are_left_alone() -> None:
    """Another program (here: a KaroX 5 bridge) holds 443 and 8443: KaroX takes 10000."""
    fake = FakeTailscale(serve=merged(busy(443, "http://127.0.0.1:9000"), busy(8443, TARGET[:-1])))
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    assert tunnel.url == f"https://{HOST}:10000"
    tunnel.stop()
    offs = [c for c in fake.calls if c[-1] == "off"]
    assert offs == [["funnel", "--https=10000", "--set-path=/", TARGET, "off"]]


def test_a_route_left_by_a_killed_karox_is_reused() -> None:
    fake = FakeTailscale(serve=busy(443, TARGET))
    assert open_tailscale(8765, exe="ts", run=fake).url == f"https://{HOST}"


def test_serve_stays_in_the_tailnet() -> None:
    fake = FakeTailscale(serve=busy(443, TARGET, funnel=True))  # public: not ours to reuse
    tunnel = open_tailscale(8765, public=False, exe="ts", run=fake)
    assert not tunnel.public
    assert tunnel.url == f"https://{HOST}:8443"
    assert fake.calls[-1] == ["serve", "--bg", "--yes", "--https=8443", TARGET]


def test_no_free_port() -> None:
    config = merged(*(busy(p, "http://127.0.0.1:1") for p in (443, 8443, 10000)))
    assert free_port(config, HOST, TARGET, public=True) is None
    with pytest.raises(TunnelError, match="already"):
        open_tailscale(8765, exe="ts", run=FakeTailscale(serve=config))


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeTailscale(state="NeedsLogin"), "not connected"),
        (FakeTailscale(state="NoState"), "not connected"),
        (FakeTailscale(dns=""), "MagicDNS"),
        (
            FakeTailscale(
                publish=(1, "Funnel not available; to enable: https://login.tailscale.com/f/x")
            ),
            "Open https://login.tailscale.com/f/x",
        ),
        (FakeTailscale(publish=(TIMED_OUT, "")), "did not finish"),
    ],
)
def test_failures_say_what_to_do(fake: FakeTailscale, message: str) -> None:
    with pytest.raises(TunnelError, match=message.replace(".", r"\.").replace("?", r"\?")):
        open_tailscale(8765, exe="ts", run=fake)


def test_tailscale_not_answering() -> None:
    def down(argv: list[str], timeout_s: float) -> tuple[int, str]:
        return 1, "failed to connect to local tailscaled"

    with pytest.raises(TunnelError, match="Tailscale app"):
        open_tailscale(8765, exe="ts", run=down)


def test_tunnel_needs_http(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["mcp", "--model", "m", "--tunnel", "tailscale", "--no-mcp"])
    assert code == 2
    assert "--tunnel needs --http" in capsys.readouterr().err


@pytest.mark.parametrize("answer", [(1, "connection refused"), (TIMED_OUT, ""), (0, "not json")])
def test_routes_that_cannot_be_read_are_not_touched(answer: tuple[int, str]) -> None:
    """Another program's route on 443 must not be replaced because the read failed."""
    fake = FakeTailscale(serve=busy(443, "http://127.0.0.1:9000"), serve_status=answer)
    with pytest.raises(TunnelError, match="does not touch them"):
        open_tailscale(8765, exe="ts", run=fake)
    assert not any(c[0] in ("funnel", "serve") and c[1] != "status" for c in fake.calls)


def test_an_empty_config_is_free() -> None:
    fake = FakeTailscale(serve_status=(0, "{}"))
    assert open_tailscale(8765, exe="ts", run=fake).url == f"https://{HOST}"


def test_ports_of_a_foreground_funnel_are_skipped() -> None:
    config = {"Foreground": {"sess1": busy(443, TARGET)}}  # even one routing to our target
    assert free_port(config, HOST, TARGET, public=True) == 8443


def test_stop_leaves_a_route_someone_replaced() -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    fake.serve = busy(443, "http://127.0.0.1:3000")  # `tailscale funnel --bg 3000` meanwhile
    tunnel.stop()
    assert not [c for c in fake.calls if c[-1] == "off"]


@pytest.mark.parametrize("applied", [True, False])
def test_a_publish_that_timed_out_is_undone_if_it_was_applied(applied: bool) -> None:
    fake = FakeTailscale(publish=(TIMED_OUT, ""), applied=applied)
    with pytest.raises(TunnelError, match="did not finish"):
        open_tailscale(8765, exe="ts", run=fake)
    offs = [c for c in fake.calls if c[-1] == "off"]
    assert offs == ([["funnel", "--https=443", "--set-path=/", TARGET, "off"]] if applied else [])


def test_the_target_follows_the_bound_address() -> None:
    fake = FakeTailscale()
    open_tailscale(8765, local_host="100.64.0.7", exe="ts", run=fake)
    assert fake.calls[-1][-1] == "http://100.64.0.7:8765"
    open_tailscale(8766, local_host="0.0.0.0", exe="ts", run=fake)
    assert fake.calls[-1][-1] == "http://127.0.0.1:8766"


def test_keep_leaves_an_intact_route_alone() -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    before = len(fake.calls)
    assert tunnel.keep() is None
    assert fake.calls[before:] == [["serve", "status", "--json"]]


@pytest.mark.parametrize("public", [True, False])
def test_keep_puts_a_vanished_route_back(public: bool) -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, public=public, exe="ts", run=fake)
    fake.serve = {}  # Tailscale lost it, or someone ran `tailscale serve reset`
    line = tunnel.keep()
    assert line is not None
    assert "put it back" in line
    kind = "funnel" if public else "serve"
    assert fake.calls[-1] == [kind, "--bg", "--yes", "--https=443", TARGET]
    assert tunnel.keep() is None  # in place again
    tunnel.stop()
    assert fake.calls[-1] == [kind, "--https=443", "--set-path=/", TARGET, "off"]


@pytest.mark.parametrize(
    "taken",
    [
        busy(443, "http://127.0.0.1:3000"),  # another program's route
        busy(443, TARGET, funnel=False),  # our target, but someone made it tailnet-only
        {"Foreground": {"sess1": busy(443, TARGET)}},  # a foreground `tailscale funnel`
    ],
)
def test_keep_never_takes_over_a_port_someone_else_uses(taken: dict) -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    fake.serve = taken
    before = len(fake.calls)
    line = tunnel.keep()
    assert line is not None
    assert "serves something else" in line
    assert fake.calls[before:] == [["serve", "status", "--json"]]


def test_keep_says_what_it_could_not_do() -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    fake.serve_status = (1, "failed to connect to local tailscaled")
    before = len(fake.calls)
    line = tunnel.keep()
    assert line is not None
    assert "does not touch them" in line
    assert fake.calls[before:] == [["serve", "status", "--json"]]
    fake.serve_status = None
    fake.serve = {}
    fake.applied = False
    fake.publish = (1, "Funnel not available; to enable: https://login.tailscale.com/f/x")
    line = tunnel.keep()
    assert line is not None
    assert line.startswith("could not put the Tailscale route back: Funnel is not enabled")


def test_keep_does_nothing_once_stopped() -> None:
    fake = FakeTailscale()
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    tunnel.stop()
    before = len(fake.calls)
    assert tunnel.keep() is None
    assert fake.calls[before:] == []


def test_stop_waits_for_a_keep_in_flight() -> None:
    """A re-apply still running when KaroX stops must not leave the route behind."""
    fake = FakeTailscale()
    publishing, release = threading.Event(), threading.Event()
    slow = False

    def run(argv: list[str], timeout_s: float) -> tuple[int, str]:
        if slow and "--bg" in argv:
            publishing.set()
            release.wait(10)
        return fake(argv, timeout_s)

    tunnel = open_tailscale(8765, exe="ts", run=run)
    slow = True
    fake.serve = {}
    keeping = threading.Thread(target=tunnel.keep)
    keeping.start()
    assert publishing.wait(10)
    stopping = threading.Thread(target=tunnel.stop)
    stopping.start()
    time.sleep(0.1)
    assert stopping.is_alive()  # waits for the keep
    release.set()
    keeping.join(10)
    stopping.join(10)
    assert fake.calls[-1] == ["funnel", "--https=443", "--set-path=/", TARGET, "off"]
    assert fake.serve.get("Web") == {}


def test_the_keeper_prints_each_problem_once(capsys: pytest.CaptureFixture[str]) -> None:
    from karox6.cli import _keep_tunnel

    class Done(Exception):
        pass

    answers = [None, "a", "a", None, "a", "b", "b"]

    class Fake:
        def keep(self) -> str | None:
            if not answers:
                raise Done
            return answers.pop(0)

    with pytest.raises(Done):
        asyncio.run(_keep_tunnel(Fake(), interval=0))
    assert capsys.readouterr().err.splitlines() == ["karox mcp: a", "karox mcp: a", "karox mcp: b"]


def test_the_server_checks_its_tunnel_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox6 import cli
    from karox6.mcp.tunnel import Tunnel

    events: list[str] = []
    checked = asyncio.Event()
    loops: list[asyncio.AbstractEventLoop] = []

    def keep() -> str | None:
        events.append("keep")
        if events.count("keep") == 2:
            loops[0].call_soon_threadsafe(checked.set)
        return None

    tunnel = Tunnel("https://box", True, lambda: events.append("stop"), keep)

    async def fake_open(port: int, host: str, *, public: bool) -> Tunnel:
        return tunnel

    real_keep = cli._keep_tunnel
    monkeypatch.setattr(cli, "_open_tunnel", fake_open)
    monkeypatch.setattr(cli, "_keep_tunnel", lambda t: real_keep(t, interval=0.01))
    # The server drops both from the environment; monkeypatch puts them back afterwards.
    monkeypatch.delenv("KAROX_MCP_TOKEN", raising=False)
    monkeypatch.delenv("KAROX_API_KEY", raising=False)
    flags = ["--direct", "--http", "127.0.0.1:0", "--tunnel", "tailscale", "--no-mcp"]
    args = cli._parser().parse_args(
        ["mcp", *flags, "--home", str(tmp_path / "home"), "--root", str(tmp_path)]
    )

    async def scenario() -> None:
        loops.append(asyncio.get_running_loop())
        server = asyncio.ensure_future(cli._mcp_async(args))
        await asyncio.wait_for(checked.wait(), 30)
        server.cancel()
        await asyncio.wait([server])

    asyncio.run(scenario())
    assert events[:2] == ["keep", "keep"]
    assert events.count("stop") == 1  # the route is removed on exit
