from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.models import AccessProfile
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from karox.tool_telemetry import ToolTraceStore
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_telemetry_components.json"


class StaticSessionInfoRuntime:
    def __init__(self, inner: CompositeHostedBridge) -> None:
        self.inner = inner
        self._session_info = inner.session_info()

    def descriptors(self):
        return self.inner.descriptors()

    def session_info(self) -> dict[str, Any]:
        return dict(self._session_info)

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ):
        return self.inner.execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )


def _run(runtime, token: str, repeats: int, *, telemetry: bool, no_record: bool = False) -> float:
    env = {"KAROX_TOOL_TELEMETRY": "1" if telemetry else "0"}
    record_patch = patch.object(ToolTraceStore, "record", autospec=True) if no_record else None
    with patch.dict(os.environ, env, clear=False):
        if record_patch is None:
            app = build_proxy_asgi_app(runtime, token, inline_result_bytes=32 * 1024)
        else:
            with record_patch:
                app = build_proxy_asgi_app(runtime, token, inline_result_bytes=32 * 1024)
                requests = [
                    _tools_call(token, "karox.repo.read_file", {"path": "sample.txt"})
                    for _ in range(repeats)
                ]
                started = time.perf_counter()
                responses = _wire_requests(app, requests)
                elapsed = (time.perf_counter() - started) * 1000 / repeats
                assert all(not _jsonrpc_result(response)["isError"] for response in responses)
                return elapsed
    requests = [
        _tools_call(token, "karox.repo.read_file", {"path": "sample.txt"})
        for _ in range(repeats)
    ]
    started = time.perf_counter()
    responses = _wire_requests(app, requests)
    elapsed = (time.perf_counter() - started) * 1000 / repeats
    assert all(not _jsonrpc_result(response)["isError"] for response in responses)
    return elapsed


def test_telemetry_component_costs() -> None:
    repeats = 40
    rounds = 3
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repo"
        initialize_git_repository(repository)
        (repository / "sample.txt").write_bytes(b"telemetry component fixture\n")
        sessions = SessionStore(root / "sessions")
        session_id = "telemetry-components"
        sessions.create(repository, "telemetry components", AccessProfile.WORKSPACE_WRITE, session_id=session_id)
        core = CoreToolBridge(repository, sessions, session_id, ["karox.repo.read_file"], audit_path=root / "audit.jsonl")
        runtime = CompositeHostedBridge([core])
        static_runtime = StaticSessionInfoRuntime(runtime)
        token = "telemetry-components-token"
        samples = {"off": [], "on": [], "on_no_sqlite_record": [], "on_static_revision": [], "on_static_revision_no_record": []}
        for _ in range(rounds):
            samples["off"].append(_run(runtime, token, repeats, telemetry=False))
            samples["on"].append(_run(runtime, token, repeats, telemetry=True))
            samples["on_no_sqlite_record"].append(_run(runtime, token, repeats, telemetry=True, no_record=True))
            samples["on_static_revision"].append(_run(static_runtime, token, repeats, telemetry=True))
            samples["on_static_revision_no_record"].append(_run(static_runtime, token, repeats, telemetry=True, no_record=True))

    medians = {key: statistics.median(values) for key, values in samples.items()}
    payload = {
        "schema_version": 1,
        "benchmark": "gpt-web-tool-telemetry-components",
        "repeats_per_round": repeats,
        "rounds": rounds,
        "per_call_ms": {key: round(value, 4) for key, value in medians.items()},
        "estimated_sqlite_record_ms": round(medians["on"] - medians["on_no_sqlite_record"], 4),
        "estimated_revision_reads_ms": round(medians["on"] - medians["on_static_revision"], 4),
        "estimated_trace_compute_ms": round(medians["on_static_revision_no_record"] - medians["off"], 4),
        "isolation": {"disposable_repository": True, "in_process_asgi": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
