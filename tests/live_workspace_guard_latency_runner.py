from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from karox.workspace_change_guard import start_workspace_change_guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_live_workspace_guard_latency.json"


def test_live_workspace_guard_latency() -> None:
    rows: list[dict[str, object]] = []
    for _ in range(5):
        started = time.perf_counter()
        guard = start_workspace_change_guard(ROOT)
        start_ms = (time.perf_counter() - started) * 1000
        time.sleep(0.05)
        finished = time.perf_counter()
        quiet = guard.finish()
        finish_ms = (time.perf_counter() - finished) * 1000
        rows.append({
            "start_ms": round(start_ms, 3),
            "finish_ms": round(finish_ms, 3),
            "supported": guard.supported,
            "quiet": quiet,
            "changed": guard.changed,
            "failed": guard.failed,
            "changed_paths": list(guard.changed_paths),
        })
        time.sleep(0.02)
    payload = {
        "schema_version": 1,
        "repository": "live-current-repository-read-only",
        "samples": rows,
        "median_start_ms": round(statistics.median(float(row["start_ms"]) for row in rows), 3),
        "median_finish_ms": round(statistics.median(float(row["finish_ms"]) for row in rows), 3),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert all(bool(row["supported"]) for row in rows)
