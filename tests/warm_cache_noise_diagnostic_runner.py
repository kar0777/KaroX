"""Explicit warm-cache watcher noise diagnostic; not auto-discovered."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from _support import ROOT  # noqa: F401
from test_plan_executor_change_guard import PlanExecutorChangeGuardTests

OUT = Path(__file__).resolve().parents[1] / "benchmarks" / "agent_throughput" / "latest_warm_cache_noise.json"


def test_warm_cache_noise_diagnostic() -> None:
    from karox.workspace_change_guard import start_workspace_change_guard as real_start

    case = PlanExecutorChangeGuardTests(methodName="test_native_quiet_second_plan_reuses_warm_identity")
    case.setUp()
    payload: dict[str, object] = {"schema_version": 1}
    try:
        baseline_guard = real_start(case.repo)
        identity = case.context._fast_revision_identity()
        payload["baseline"] = {
            "identity": identity,
            "quiet": baseline_guard.finish(),
            "changed": baseline_guard.changed,
            "failed": baseline_guard.failed,
            "paths": list(baseline_guard.changed_paths),
        }

        captured = []

        def factory(repository):
            guard = real_start(repository)
            captured.append(guard)
            return guard

        try:
            with mock.patch("karox.plan_executor.start_workspace_change_guard", side_effect=factory):
                result = case.executor.execute(case._plan(), "warm-noise-diag")
            payload["plan"] = {"ok": result.get("ok"), "error": None}
        except Exception as exc:  # diagnostic only
            payload["plan"] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "details": getattr(exc, "details", None),
            }
        payload["guards"] = [
            {
                "changed": guard.changed,
                "failed": guard.failed,
                "closed": getattr(guard, "_closed", None),
                "paths": list(guard.changed_paths),
            }
            for guard in captured
        ]
        case.executor._invalidate_read_only_cache()
    finally:
        case.tearDown()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
