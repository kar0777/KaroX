from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.models import AccessProfile
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_telemetry_overhead.json"


def _run(runtime: CompositeHostedBridge, token: str, enabled: bool, repeats: int) -> float:
    value = "1" if enabled else "0"
    with patch.dict(os.environ, {"KAROX_TOOL_TELEMETRY": value}, clear=False):
        app = build_proxy_asgi_app(runtime, token, inline_result_bytes=32 * 1024)
    requests = [
        _tools_call(token, "karox.repo.read_file", {"path": "sample.txt"})
        for _ in range(repeats)
    ]
    started = time.perf_counter()
    responses = _wire_requests(app, requests)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert all(not _jsonrpc_result(response)["isError"] for response in responses)
    return elapsed_ms / repeats


def test_gpt_web_telemetry_overhead() -> None:
    repeats = 50
    rounds = 3
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repo"
        initialize_git_repository(repository)
        (repository / "sample.txt").write_bytes(b"telemetry fixture\n")
        sessions = SessionStore(root / "sessions")
        session_id = "telemetry-overhead"
        sessions.create(
            repository,
            "telemetry overhead",
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
        runtime = CompositeHostedBridge([core])
        token = "telemetry-overhead-token"
        on_samples: list[float] = []
        off_samples: list[float] = []
        for _ in range(rounds):
            off_samples.append(_run(runtime, token, False, repeats))
            on_samples.append(_run(runtime, token, True, repeats))

    on_median = statistics.median(on_samples)
    off_median = statistics.median(off_samples)
    payload = {
        "schema_version": 1,
        "benchmark": "gpt-web-tool-telemetry-overhead",
        "repeats_per_round": repeats,
        "rounds": rounds,
        "telemetry_on_per_call_ms": round(on_median, 4),
        "telemetry_off_per_call_ms": round(off_median, 4),
        "telemetry_overhead_ms": round(on_median - off_median, 4),
        "telemetry_overhead_pct_of_on": round((on_median - off_median) / on_median * 100, 1),
        "isolation": {"disposable_repository": True, "in_process_asgi": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
