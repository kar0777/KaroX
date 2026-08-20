from __future__ import annotations

import json
from pathlib import Path

import pytest

from _support import SRC  # noqa: F401
from karox.tool_telemetry import ToolTraceStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_live_tool_trace_snapshot.json"
SESSION_ID = "web-saved-877046fafcba4c7dfb81c6b0"


def test_snapshot_live_tool_traces() -> None:
    store = ToolTraceStore(SESSION_ID)
    try:
        rows = list(store.list(limit=120))
    finally:
        store.close()
    interesting = [
        row
        for row in rows
        if row.get("tool_name")
        in {
            "karox.task.execute_plan",
            "karox.runtime.status",
            "karox.repo.search",
            "karox.repo.read_file",
            "karox.repo.read_lines",
            "karox.browser.tabs",
            "karox.bridge.diagnostics",
        }
    ]
    payload = {
        "schema_version": 1,
        "session_id": SESSION_ID,
        "rows_considered": len(rows),
        "interesting": interesting[:80],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert rows


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
