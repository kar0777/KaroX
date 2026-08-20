"""Explicit execute_plan overhead diagnostic; not auto-discovered by pytest."""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_agent_throughput_plan_diag", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _median(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row[key]) for row in rows]
    return round(statistics.median(values), 3)


def test_execute_plan_overhead_diagnostic() -> None:
    module = _load_module()
    case = module.CASES["01"]
    payload: dict[str, Any] = {"schema_version": 1, "case": "01", "scenarios": {}}
    for scenario, noise_files in (("clean", 0), ("dirty", 200)):
        rows: list[dict[str, Any]] = []
        for iteration in range(3):
            with module._runtime_fixture(case, noise_files=noise_files) as fixture:
                module._instrument_method(fixture.executor, "_tool_call", fixture.timings, "plan_tool_call_ms")
                module._instrument_method(
                    fixture.executor,
                    "_apply_output_policy",
                    fixture.timings,
                    "output_policy_ms",
                )
                module._instrument_method(
                    fixture.executor,
                    "_delegate_names",
                    fixture.timings,
                    "delegate_names_ms",
                )
                module._instrument_method(fixture.executor, "_parse", fixture.timings, "parse_ms")
                module._instrument_method(fixture.executor, "_digest", fixture.timings, "digest_ms")
                module._instrument_method(fixture.delegate.inner, "_core", fixture.timings, "bridge_core_ms")
                module._instrument_method(
                    fixture.delegate.inner,
                    "_definitions",
                    fixture.timings,
                    "bridge_definitions_ms",
                )
                module._instrument_method(fixture.delegate.inner, "_record", fixture.timings, "bridge_record_ms")
                row = module._plan_once(fixture, case, iteration)
                for metric in (
                    "plan_tool_call_ms",
                    "output_policy_ms",
                    "delegate_names_ms",
                    "parse_ms",
                    "digest_ms",
                    "bridge_core_ms",
                    "bridge_definitions_ms",
                    "bridge_record_ms",
                ):
                    row[metric] = fixture.timings.total(metric)
                accounted = (
                    row["server_tool_ms"]
                    + row["fast_identity_ms"]
                    + row["journal_save_ms"]
                    + row["journal_mutate_ms"]
                    + row["artifact_put_ms"]
                    + row["checkpoint_ms"]
                )
                row["unaccounted_ms"] = round(max(0.0, row["wall_ms"] - accounted), 3)
                rows.append(row)
        payload["scenarios"][scenario] = {
            "samples": rows,
            "median": {
                key: _median(rows, key)
                for key in (
                    "wall_ms",
                    "server_tool_ms",
                    "fast_identity_ms",
                    "journal_save_ms",
                    "journal_mutate_ms",
                    "artifact_put_ms",
                    "checkpoint_ms",
                    "plan_tool_call_ms",
                    "output_policy_ms",
                    "delegate_names_ms",
                    "parse_ms",
                    "digest_ms",
                    "bridge_core_ms",
                    "bridge_definitions_ms",
                    "bridge_record_ms",
                    "unaccounted_ms",
                )
            },
        }
    result_path = ROOT / "benchmarks" / "agent_throughput" / "latest_plan_overhead_diagnostic.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("PLAN_OVERHEAD_DIAGNOSTIC=" + json.dumps(payload["scenarios"], sort_keys=True))
