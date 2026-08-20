from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.models import AccessProfile
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from test_hosted_bridge import _WireServer, _jsonrpc_result, _tools_call, _wire_requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_end_to_end_latency.json"


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples), 4)


def _direct_samples(call, repeats: int) -> list[float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def test_gpt_web_end_to_end_latency() -> None:
    repeats = 50
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repo"
        initialize_git_repository(repository)
        (repository / "sample.txt").write_bytes(b"hello from latency fixture\n")
        sessions = SessionStore(root / "sessions")
        session_id = "latency-session"
        sessions.create(
            repository,
            "latency benchmark",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        core = CoreToolBridge(
            repository,
            sessions,
            session_id,
            ["karox.repo.read_file"],
            audit_path=root / "audit.jsonl",
        )
        composite = CompositeHostedBridge([core])
        arguments = {"path": "sample.txt"}

        core_samples = _direct_samples(
            lambda: core.execute("karox.repo.read_file", arguments), repeats
        )
        composite_samples = _direct_samples(
            lambda: composite.execute("karox.repo.read_file", arguments), repeats
        )

        token = "latency-benchmark-token"
        app = build_proxy_asgi_app(composite, token, inline_result_bytes=32 * 1024)
        requests = [
            _tools_call(token, "karox.repo.read_file", arguments)
            for _ in range(repeats)
        ]
        started = time.perf_counter()
        asgi_responses = _wire_requests(app, requests)
        asgi_total_ms = (time.perf_counter() - started) * 1000
        assert all(not _jsonrpc_result(response)["isError"] for response in asgi_responses)

        http_app = build_proxy_asgi_app(composite, token, inline_result_bytes=32 * 1024)
        server = _WireServer(http_app)
        try:
            url = f"http://127.0.0.1:{server.port}/mcp"
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "karox.repo.read_file", "arguments": arguments},
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            http_samples: list[float] = []
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                for _ in range(repeats):
                    started = time.perf_counter()
                    response = client.post(url, json=body, headers=headers)
                    http_samples.append((time.perf_counter() - started) * 1000)
                    assert response.status_code == 200
        finally:
            server.close()

    core_median = _median_ms(core_samples)
    composite_median = _median_ms(composite_samples)
    asgi_per_call = round(asgi_total_ms / repeats, 4)
    http_median = _median_ms(http_samples)
    payload = {
        "schema_version": 1,
        "benchmark": "gpt-web-end-to-end-latency-breakdown",
        "repeats": repeats,
        "core_bridge_median_ms": core_median,
        "composite_median_ms": composite_median,
        "mcp_asgi_per_call_ms": asgi_per_call,
        "localhost_http_median_ms": http_median,
        "estimated_composite_overhead_ms": round(composite_median - core_median, 4),
        "estimated_mcp_asgi_overhead_ms": round(asgi_per_call - composite_median, 4),
        "estimated_http_overhead_ms": round(http_median - asgi_per_call, 4),
        "isolation": {
            "disposable_repository": True,
            "localhost_only": True,
            "live_bridge_touched": False,
            "saved_profile_touched": False,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
