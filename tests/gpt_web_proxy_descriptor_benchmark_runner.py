from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from _support import SRC  # noqa: F401
from karox.hosted_bridge import ProxyToolDescriptor
from karox.proxy_server import build_proxy_asgi_app
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_gpt_web_proxy_descriptor_benchmark.json"


class CountingRuntime:
    def __init__(self) -> None:
        self.descriptor_calls = 0
        self.execute_calls = 0

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self.descriptor_calls += 1
        return [
            ProxyToolDescriptor(
                name="karox.repo.read_file",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
                read_only=True,
            )
        ]

    def session_info(self) -> dict[str, Any]:
        return {"revision": 1}

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del idempotency_key, deadline_seconds
        self.execute_calls += 1
        return {"ok": True, "data": {"path": arguments.get("path", ""), "content": "x"}}


def test_proxy_descriptor_hot_path() -> None:
    runtime = CountingRuntime()
    token = "descriptor-benchmark-token"
    app = build_proxy_asgi_app(runtime, token)
    requests = [
        _tools_call(token, "karox.repo.read_file", {"path": f"file-{index}.txt"})
        for index in range(100)
    ]
    started = time.perf_counter()
    responses = _wire_requests(app, requests)
    wall_ms = (time.perf_counter() - started) * 1000
    assert len(responses) == 100
    assert all(not _jsonrpc_result(response)["isError"] for response in responses)
    assert runtime.execute_calls == 100
    payload = {
        "schema_version": 1,
        "benchmark": "gpt-web-proxy-descriptor-hot-path",
        "calls": 100,
        "descriptor_calls": runtime.descriptor_calls,
        "execute_calls": runtime.execute_calls,
        "wall_ms": round(wall_ms, 3),
        "per_call_ms": round(wall_ms / 100, 4),
        "isolation": {"in_process_asgi": True, "live_bridge_touched": False, "saved_profile_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
