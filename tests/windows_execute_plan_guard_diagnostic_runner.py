from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "agent_throughput" / "throughput_benchmark.py"
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_execute_plan_guard.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("karox_guard_throughput_diag", BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load throughput benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_execute_plan_guard_diagnostic() -> None:
    from karox.workspace_change_guard import start_workspace_change_guard as real_start

    module = _load_module()
    case = module.CASES["01"]
    payload: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "windows-execute-plan-change-guard-diagnostic",
        "scenarios": {},
    }
    for scenario, noise_files in (("clean", 0), ("dirty", 200)):
        captured = []

        def factory(repository):
            guard = real_start(repository)
            captured.append(guard)
            return guard

        with patch("karox.plan_executor.start_workspace_change_guard", side_effect=factory):
            with module._runtime_fixture(case, noise_files=noise_files) as fixture:
                control_values: list[dict[str, str]] = []
                original_control = fixture.context._git_control_identity

                def control():
                    value = original_control()
                    control_values.append(value)
                    return value

                fixture.context._git_control_identity = control
                row = module._plan_once(fixture, case, 1)
        assert captured
        guard = captured[-1]
        payload["scenarios"][scenario] = {
            "fast_identity_ms": row["fast_identity_ms"],
            "wall_ms": row["wall_ms"],
            "guard_supported": guard.supported,
            "guard_changed": guard.changed,
            "guard_failed": guard.failed,
            "ignored_git_events": guard.ignored_git_events,
            "changed_paths": list(guard.changed_paths),
            "control_samples": len(control_values),
            "control_all_equal": bool(control_values) and all(value == control_values[0] for value in control_values[1:]),
        }
    payload["isolation"] = {"disposable_repositories": True, "live_bridge_touched": False}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
