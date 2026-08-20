"""Explicit warm read-only execute_plan cache benchmark; not auto-discovered."""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_warm_plan_cache.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_warm_plan_cache", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _median(rows: list[dict[str, Any]], key: str) -> float:
    return round(statistics.median(float(row[key]) for row in rows), 3)


def test_warm_read_only_plan_cache() -> None:
    module = _load_module()
    case = module.CASES["01"]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "warm-read-only-execute-plan-cache",
        "isolation": {
            "disposable_repositories": True,
            "live_bridge_touched": False,
        },
        "scenarios": {},
    }
    for scenario, noise_files in (("clean", 0), ("dirty", 200)):
        cold_rows: list[dict[str, Any]] = []
        warm_rows: list[dict[str, Any]] = []
        for iteration in range(3):
            with module._runtime_fixture(case, noise_files=noise_files) as fixture:
                try:
                    cold_rows.append(module._plan_once(fixture, case, iteration * 2))
                    warm_rows.append(module._plan_once(fixture, case, iteration * 2 + 1))
                finally:
                    fixture.executor._invalidate_read_only_cache()
        payload["scenarios"][scenario] = {
            "cold_median": {
                "wall_ms": _median(cold_rows, "wall_ms"),
                "fast_identity_ms": _median(cold_rows, "fast_identity_ms"),
                "server_tool_ms": _median(cold_rows, "server_tool_ms"),
            },
            "warm_median": {
                "wall_ms": _median(warm_rows, "wall_ms"),
                "fast_identity_ms": _median(warm_rows, "fast_identity_ms"),
                "server_tool_ms": _median(warm_rows, "server_tool_ms"),
            },
            "warm_wall_reduction_pct": round(
                100.0
                * (1.0 - _median(warm_rows, "wall_ms") / _median(cold_rows, "wall_ms")),
                1,
            ),
            "cold_samples": cold_rows,
            "warm_samples": warm_rows,
        }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
