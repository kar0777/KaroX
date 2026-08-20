"""Explicit pytest runner for the isolated agent-throughput benchmark.

The filename intentionally does not start with test_ so normal repository test
discovery does not run this performance workload.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_agent_throughput_benchmark", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_clean_and_dirty() -> None:
    module = _load_module()
    payload = module.run_benchmark(
        case_ids=("01", "02", "03", "05", "06"),
        repeats=1,
        scenarios=("clean", "dirty"),
        noise_files=200,
    )
    assert payload["benchmark"] == "karox-agent-throughput"
    assert payload["isolation"]["live_bridge_touched"] is False
    assert len(payload["cases"]) == 10
    for case in payload["cases"]:
        summary = case["summary"]
        assert summary["manual_low_level"]["median_mcp_round_trips"] >= 3
        assert summary["execute_plan"]["median_mcp_round_trips"] == 1
        assert summary["repo_inspect_warm"]["samples"] == 1
    print("AGENT_THROUGHPUT_RESULT=" + json.dumps(module.compact_report(payload), sort_keys=True))
