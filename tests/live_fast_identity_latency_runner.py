from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from karox.artifacts import ArtifactStore
from karox.repo_context import RepositoryContextEngine

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_live_fast_identity_latency.json"


def test_live_fast_identity_latency() -> None:
    context = RepositoryContextEngine(ROOT, ArtifactStore("live-fast-identity-diagnostic"), policy_profile="workspace_write")
    rows: list[dict[str, object]] = []
    for _ in range(5):
        started = time.perf_counter()
        identity = context._fast_revision_identity()
        elapsed_ms = (time.perf_counter() - started) * 1000
        rows.append({
            "elapsed_ms": round(elapsed_ms, 3),
            "revision": identity.get("revision"),
            "dirty_count": len(identity.get("dirty", [])) if isinstance(identity.get("dirty"), list) else None,
        })
    payload = {
        "schema_version": 1,
        "repository": "live-current-repository-read-only",
        "median_ms": round(statistics.median(float(row["elapsed_ms"]) for row in rows), 3),
        "samples": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert all(float(row["elapsed_ms"]) >= 0 for row in rows)
