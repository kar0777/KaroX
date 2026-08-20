from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_core_read_search_components.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_core_component_diag", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wrap(obj: Any, name: str, values: dict[str, list[float]], metric: str) -> None:
    original = getattr(obj, name)

    def wrapped(*args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            values.setdefault(metric, []).append((time.perf_counter() - started) * 1000)

    setattr(obj, name, wrapped)


def _wrap_handler(runtime: Any, name: str, values: dict[str, list[float]]) -> None:
    original: Callable[..., Any] = runtime._handlers[name]

    def wrapped(*args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            values.setdefault(f"handler:{name}", []).append((time.perf_counter() - started) * 1000)

    runtime._handlers[name] = wrapped


def _summary(values: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for key, samples in sorted(values.items()):
        result[key] = {
            "count": len(samples),
            "total_ms": round(sum(samples), 4),
            "median_ms": round(statistics.median(samples), 4),
        }
    return result


def test_core_read_search_components() -> None:
    module = _load_module()
    case = module.CASES["01"]
    rows: list[dict[str, object]] = []
    for iteration in range(3):
        with module._runtime_fixture(case, noise_files=0) as fixture:
            values: dict[str, list[float]] = {}
            bridge = fixture.delegate.inner
            runtime = bridge._runtime
            _wrap(runtime, "_load_session", values, "core._load_session")
            _wrap(runtime, "_audit", values, "core._audit")
            _wrap(runtime, "_validate_arguments", values, "core._validate_arguments")
            _wrap(runtime, "_apply_smart_stop", values, "core._apply_smart_stop")
            _wrap(runtime.policy, "require", values, "policy.require")
            _wrap(fixture.sessions, "load", values, "sessions.load")
            _wrap(fixture.sessions, "validate_repository", values, "sessions.validate_repository")
            _wrap_handler(runtime, "repo.search", values)
            _wrap_handler(runtime, "repo.read_file", values)

            fixture.delegate.reset()
            started = time.perf_counter()
            fixture.delegate.execute("karox.repo.search", {"query": case.search_query})
            for path in case.read_paths:
                fixture.delegate.execute("karox.repo.read_file", {"path": path})
            wall_ms = (time.perf_counter() - started) * 1000
            rows.append(
                {
                    "iteration": iteration,
                    "wall_ms": round(wall_ms, 4),
                    "calls": list(fixture.delegate.calls),
                    "components": _summary(values),
                }
            )

    payload = {
        "schema_version": 1,
        "benchmark": "core-read-search-component-breakdown",
        "rows": rows,
        "median_wall_ms": round(statistics.median(float(row["wall_ms"]) for row in rows), 4),
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
