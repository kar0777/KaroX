#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "tailscale_readiness.py"

spec = importlib.util.spec_from_file_location("tailscale_readiness", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main() -> int:
    direct = module.parse_status(
        {
            "BackendState": "Running",
            "Self": {
                "DNSName": "desktop.example-tailnet.ts.net.",
                "HostName": "desktop",
                "TailscaleIPs": ["100.64.0.1"],
            },
        },
        "tailscale",
    )
    assert direct["ready"] is True
    assert direct["dnsName"] == "desktop.example-tailnet.ts.net"
    assert direct["baseUrl"] == "https://desktop.example-tailnet.ts.net"

    derived = module.parse_status(
        {
            "BackendState": "Running",
            "MagicDNSSuffix": "example-tailnet.ts.net",
            "Self": {"HostName": "desktop", "DNSName": ""},
        },
        "tailscale",
    )
    assert derived["ready"] is True
    assert derived["dnsName"] == "desktop.example-tailnet.ts.net"

    login = module.parse_status(
        {
            "BackendState": "NeedsLogin",
            "AuthURL": "https://login.tailscale.com/a/example",
            "Self": {},
        },
        "tailscale",
    )
    assert login["ready"] is False
    assert login["authUrl"].startswith("https://login.tailscale.com/")
    assert "login" in login["error"].lower()

    sequence = iter(
        [
            {"ready": False, "backendState": "Starting", "error": "starting"},
            {
                "ready": True,
                "backendState": "Running",
                "dnsName": "desktop.example-tailnet.ts.net",
                "baseUrl": "https://desktop.example-tailnet.ts.net",
                "error": "",
            },
        ]
    )
    original_query = module.query_status
    original_sleep = module.time.sleep
    try:
        module.query_status = lambda: next(sequence)
        module.time.sleep = lambda _seconds: None
        waited = module.wait_until_ready(5, 0.2)
    finally:
        module.query_status = original_query
        module.time.sleep = original_sleep
    assert waited["ready"] is True
    assert waited["attempts"] == 2

    print("KaroX Tailscale readiness checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
