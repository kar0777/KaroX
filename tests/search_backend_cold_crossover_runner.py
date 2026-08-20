"""Explicit cold small-tree vs git-grep crossover benchmark."""
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
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_search_backend_cold_crossover.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_search_cold_crossover", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _one(module, case, noise_files: int, *, force_git: bool) -> float:
    with module._runtime_fixture(case, noise_files=noise_files) as fixture:
        runtime = fixture.delegate.inner._core()
        arguments = {"query": case.search_query}
        started = time.perf_counter()
        if force_git:
            with (
                mock.patch.object(runtime, "_search_ripgrep", return_value=None),
                mock.patch.object(runtime, "_search_small_tree", return_value=None),
            ):
                result = runtime._search(arguments, 30.0)
        else:
            with mock.patch.object(runtime, "_search_ripgrep", return_value=None):
                result = runtime._search(arguments, 30.0)
        elapsed = (time.perf_counter() - started) * 1000
        if result.get("match_count") != 6:
            raise AssertionError(result)
        return elapsed


def test_search_backend_cold_crossover() -> None:
    module = _load_module()
    case = module.CASES["01"]
    rows: dict[str, object] = {}
    for noise_files in (40, 100, 150, 200, 250):
        small = [_one(module, case, noise_files, force_git=False) for _ in range(3)]
        git = [_one(module, case, noise_files, force_git=True) for _ in range(3)]
        rows[str(noise_files)] = {
            "small_tree_median_ms": round(statistics.median(small), 3),
            "git_grep_median_ms": round(statistics.median(git), 3),
            "small_samples_ms": [round(value, 3) for value in small],
            "git_samples_ms": [round(value, 3) for value in git],
        }
    OUTPUT.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "benchmark": "search-backend-cold-crossover",
                "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
                "sizes": rows,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
