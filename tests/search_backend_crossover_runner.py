"""Explicit adaptive search backend benchmark; not auto-discovered by pytest."""
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
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_search_backend_crossover.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_search_backend_crossover", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _semantic(result: dict[str, object]) -> tuple[tuple[object, ...], ...]:
    matches = result.get("matches")
    if not isinstance(matches, list):
        return ()
    rows: list[tuple[object, ...]] = []
    for item in matches:
        if isinstance(item, dict):
            rows.append((item.get("path"), item.get("line"), item.get("text"), item.get("clipped")))
    return tuple(rows)


def _timed(callable_, repeats: int = 5) -> tuple[float, dict[str, object]]:
    samples: list[float] = []
    last: dict[str, object] = {}
    for _ in range(repeats):
        started = time.perf_counter()
        last = dict(callable_())
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 4), last


def test_search_backend_crossover() -> None:
    module = _load_module()
    case = module.CASES["01"]
    payload: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "repo-search-backend-crossover",
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
        "sizes": {},
    }
    for noise_files in (40, 250, 1000, 2000):
        with module._runtime_fixture(case, noise_files=noise_files) as fixture:
            runtime = fixture.delegate.inner._core()
            arguments = {"query": case.search_query}

            rg_ms, rg = _timed(lambda: runtime._search(arguments, 30.0))
            with mock.patch.object(runtime, "_search_ripgrep", return_value=None):
                git_ms, git = _timed(lambda: runtime._search(arguments, 30.0))
            with (
                mock.patch.object(runtime, "_search_ripgrep", return_value=None),
                mock.patch.object(runtime, "_search_git_grep", return_value=None),
            ):
                python_ms, python = _timed(lambda: runtime._search(arguments, 30.0))

            rg_semantic = _semantic(rg)
            git_semantic = _semantic(git)
            python_semantic = _semantic(python)
            payload["sizes"][str(noise_files)] = {
                "rg_ms": rg_ms,
                "git_grep_ms": git_ms,
                "python_ms": python_ms,
                "rg_backend": rg.get("backend"),
                "git_backend": git.get("backend"),
                "python_backend": python.get("backend"),
                "rg_match_count": rg.get("match_count"),
                "git_match_count": git.get("match_count"),
                "python_match_count": python.get("match_count"),
                "rg_truncated": rg.get("truncated"),
                "git_truncated": git.get("truncated"),
                "python_truncated": python.get("truncated"),
                "git_matches_rg": git_semantic == rg_semantic,
                "python_matches_rg": python_semantic == rg_semantic,
            }

    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
