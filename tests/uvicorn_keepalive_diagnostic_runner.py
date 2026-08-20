"""Explicit local diagnostic for the installed uvicorn keep-alive defaults."""
from __future__ import annotations

import json
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]


async def _unused_app(scope, receive, send):
    del scope, receive, send


def test_installed_uvicorn_keepalive_default() -> None:
    config = uvicorn.Config(app=_unused_app, host="127.0.0.1", port=0, access_log=False)
    payload = {
        "uvicorn_version": getattr(uvicorn, "__version__", "unknown"),
        "timeout_keep_alive": config.timeout_keep_alive,
        "timeout_notify": config.timeout_notify,
    }
    output = ROOT / "benchmarks" / "agent_throughput" / "latest_uvicorn_keepalive.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("UVICORN_KEEPALIVE=" + json.dumps(payload, sort_keys=True))
    assert isinstance(config.timeout_keep_alive, (int, float))
