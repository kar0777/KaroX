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
    ) -> None:
        self.state = state
        self.dns = dns
        self.serve = serve or {}
        self.publish = publish
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append(argv[1:])
        if argv[1:] == ["status", "--json"]:
            return 0, json.dumps({"BackendState": self.state, "Self": {"DNSName": self.dns}})
        if argv[1:] == ["serve", "status", "--json"]:
            return 0, json.dumps(self.serve)
        if argv[-1] == "off":
            return 0, ""
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
    assert fake.calls[-1] == ["funnel", "--https=443", TARGET, "off"]


def test_routes_of_other_programs_are_left_alone() -> None:
    """Another program (here: a KaroX 5 bridge) holds 443 and 8443: KaroX takes 10000."""
    fake = FakeTailscale(serve=merged(busy(443, "http://127.0.0.1:9000"), busy(8443, TARGET[:-1])))
    tunnel = open_tailscale(8765, exe="ts", run=fake)
    assert tunnel.url == f"https://{HOST}:10000"
    tunnel.stop()
    offs = [c for c in fake.calls if c[-1] == "off"]
    assert offs == [["funnel", "--https=10000", TARGET, "off"]]


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
