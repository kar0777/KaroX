"""Explicit read-only plan guard component benchmark; not auto-discovered."""
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
OUTPUT = ROOT / "benchmarks" / "agent_throughput" / "latest_plan_guard_components.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_plan_guard_components", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _median(rows: list[dict[str, float]], key: str) -> float:
    return round(statistics.median(row[key] for row in rows), 3)


def test_plan_guard_components() -> None:
    module = _load_module()
    import karox.plan_executor as plan_executor_module

    original_start = plan_executor_module.start_workspace_change_guard
    case = module.CASES["01"]
    payload: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "read-only-plan-guard-components",
        "isolation": {"disposable_repositories": True, "live_bridge_touched": False},
        "scenarios": {},
    }
    for scenario, noise_files in (("clean", 0), ("dirty", 200)):
        rows: list[dict[str, float]] = []
        for iteration in range(3):
            with module._runtime_fixture(case, noise_files=noise_files) as fixture:
                timings = {"guard_start_ms": 0.0, "guard_finish_ms": 0.0, "git_control_ms": 0.0}
                original_control = fixture.context._git_control_identity

                def control_wrapper():
                    started = time.perf_counter()
                    try:
                        return original_control()
                    finally:
                        timings["git_control_ms"] += (time.perf_counter() - started) * 1000

                def start_wrapper(repository: Path):
                    started = time.perf_counter()
                    guard = original_start(repository)
                    timings["guard_start_ms"] += (time.perf_counter() - started) * 1000
                    original_finish = guard.finish

                    def finish_wrapper():
                        finish_started = time.perf_counter()
                        try:
                            return original_finish()
                        finally:
                            timings["guard_finish_ms"] += (time.perf_counter() - finish_started) * 1000

                    guard.finish = finish_wrapper  # type: ignore[method-assign]
                    return guard

                with (
                    mock.patch.object(fixture.context, "_git_control_identity", side_effect=control_wrapper),
                    mock.patch.object(plan_executor_module, "start_workspace_change_guard", side_effect=start_wrapper),
                ):
                    result = module._plan_once(fixture, case, iteration)
                timings["wall_ms"] = float(result["wall_ms"])
                rows.append(timings)
        payload["scenarios"][scenario] = {
            "median": {
                "guard_start_ms": _median(rows, "guard_start_ms"),
                "guard_finish_ms": _median(rows, "guard_finish_ms"),
                "git_control_ms": _median(rows, "git_control_ms"),
                "wall_ms": _median(rows, "wall_ms"),
            },
            "samples": rows,
        }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
