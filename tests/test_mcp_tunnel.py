from __future__ import annotations

import json

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
