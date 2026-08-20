"""Explicit identity-vs-inspect diagnostic; not auto-discovered by pytest."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
RESULT = ROOT / "benchmarks" / "agent_throughput" / "latest_identity_diagnostic.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_agent_throughput_identity", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_identity_diagnostic() -> None:
    module = _load_module()
    payload = module.run_benchmark(
        case_ids=("01", "05"),
        repeats=3,
        scenarios=("clean", "dirty"),
        noise_files=200,
    )
    RESULT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert len(payload["cases"]) == 4
    for case in payload["cases"]:
        inspect = case["summary"]["repo_inspect_cold"]
        assert inspect["median_compact_content_identity_ms"] > 0
        assert case["summary"]["execute_plan"]["median_fast_identity_ms"] > 0
