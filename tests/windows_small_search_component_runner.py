"""Explicit Windows small-tree search component benchmark; not auto-discovered."""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_small_search_components.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_windows_small_search_components", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _median(callable_, repeats: int = 5) -> float:
    values: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        callable_()
        values.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(values), 4)


def test_windows_small_search_components() -> None:
    module = _load_module()
    case = module.CASES["01"]
    with module._runtime_fixture(case, noise_files=200) as fixture:
        runtime = fixture.delegate.inner._core()
        arguments = {"query": case.search_query}
        cold_started = time.perf_counter()
        runtime._search(arguments, 30.0)
        cold_ms = round((time.perf_counter() - cold_started) * 1000, 4)
        current_ms = _median(lambda: runtime._search(arguments, 30.0))
        is_junction = getattr(Path, "is_junction", None)
        if callable(is_junction):
            with mock.patch.object(Path, "is_junction", return_value=False):
                no_junction_ms = _median(lambda: runtime._search(arguments, 30.0))
        else:
            no_junction_ms = current_ms
        with mock.patch.object(Path, "is_symlink", return_value=False):
            no_file_symlink_ms = _median(lambda: runtime._search(arguments, 30.0))
        payload = {
            "schema_version": 1,
            "benchmark": "windows-small-search-components",
            "cold_first_search_ms": cold_ms,
            "current_ms": current_ms,
            "no_path_is_junction_ms": no_junction_ms,
            "estimated_is_junction_ms": round(max(0.0, current_ms - no_junction_ms), 4),
            "no_path_is_symlink_ms": no_file_symlink_ms,
            "estimated_file_is_symlink_ms": round(
                max(0.0, current_ms - no_file_symlink_ms), 4
            ),
            "isolation": {"disposable_repository": True, "live_bridge_touched": False},
        }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
