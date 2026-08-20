"""Explicit runner for benchmarks/agent_throughput; not auto-discovered by pytest."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_agent_throughput_benchmark", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agent_throughput_matrix() -> None:
    module = _load_module()
    payload = module.run_benchmark(
        case_ids=("01", "02", "03", "05", "06"),
        repeats=1,
        scenarios=("clean", "dirty"),
        noise_files=200,
    )
    result_path = ROOT / "benchmarks" / "agent_throughput" / "latest_smoke_result.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
