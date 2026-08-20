"""Explicit PlanJournalStore component benchmark; not auto-discovered."""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_plan_journal_components.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_plan_journal_components", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _median(rows: list[dict[str, float]], key: str) -> float:
    return round(statistics.median(row[key] for row in rows), 3)


def test_plan_journal_components() -> None:
    module = _load_module()
    import karox.plan_executor as plan_module

    case = module.CASES["01"]
    rows: list[dict[str, float]] = []
    for iteration in range(5):
        with module._runtime_fixture(case, noise_files=0) as fixture:
            # Prime the warm read-only identity cache; profile the second plan.
            module._plan_once(fixture, case, iteration * 2)
            fixture.timings.reset()
            timings = {
                "atomic_json_ms": 0.0,
                "redact_ms": 0.0,
                "checksum_ms": 0.0,
            }
            original_atomic = plan_module._atomic_json
            original_redact = plan_module.redact
            original_checksum = fixture.executor.journals._checksum

            def atomic_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                try:
                    return original_atomic(*args, **kwargs)
                finally:
                    timings["atomic_json_ms"] += (time.perf_counter() - started) * 1000

            def redact_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                try:
                    return original_redact(*args, **kwargs)
                finally:
                    timings["redact_ms"] += (time.perf_counter() - started) * 1000

            def checksum_wrapper(*args: Any, **kwargs: Any) -> str:
                started = time.perf_counter()
                try:
                    return original_checksum(*args, **kwargs)
                finally:
                    timings["checksum_ms"] += (time.perf_counter() - started) * 1000

            with (
                mock.patch.object(plan_module, "_atomic_json", side_effect=atomic_wrapper),
                mock.patch.object(plan_module, "redact", side_effect=redact_wrapper),
                mock.patch.object(fixture.executor.journals, "_checksum", side_effect=checksum_wrapper),
            ):
                result = module._plan_once(fixture, case, iteration * 2 + 1)
            timings.update(
                {
                    "wall_ms": float(result["wall_ms"]),
                    "journal_save_ms": float(result["journal_save_ms"]),
                    "journal_mutate_ms": float(result["journal_mutate_ms"]),
                }
            )
            rows.append(timings)
            fixture.executor._invalidate_read_only_cache()

    payload = {
        "schema_version": 1,
        "benchmark": "plan-journal-components",
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
        "median": {
            "wall_ms": _median(rows, "wall_ms"),
            "journal_save_ms": _median(rows, "journal_save_ms"),
            "journal_mutate_ms": _median(rows, "journal_mutate_ms"),
            "atomic_json_ms": _median(rows, "atomic_json_ms"),
            "redact_ms": _median(rows, "redact_ms"),
            "checksum_ms": _median(rows, "checksum_ms"),
        },
        "samples": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
