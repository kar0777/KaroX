from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from karox import orchestration_cli


def _start_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        command="orchestrate",
        orchestrate_command="start",
        repository=tmp_path,
        objective="implement feature",
        recipe="feature",
        preset="balanced",
        risk="medium",
        orchestrator="sub:orch",
        delegate_workers=True,
        run_id=None,
        label="Feature",
        assign=["reviewer=sub:reviewer"],
        worker_effort=["orchestrator=high"],
        verification_command=['["python","-m","pytest","-q"]'],
        max_steps=32,
        max_seconds=600.0,
        isolate_implementers=True,
        baseline_cost=4.5,
        json=True,
    )


def test_detached_argv_forwards_delegation_exactly_once(tmp_path: Path) -> None:
    args = _start_args(tmp_path)
    argv = orchestration_cli._background_run_argv(args, "run-1")
    assert argv[:2] == ["orchestrate", "run"]
    assert argv.count("--delegate-workers") == 1
    assert argv[argv.index("--orchestrator") + 1] == "sub:orch"
    assert "reviewer=sub:reviewer" in argv
    assert "orchestrator=high" in argv
    assert "--isolate-implementers" in argv
    assert argv[-1] == "--json"


def test_start_does_not_preplan_or_double_call_orchestrator(tmp_path: Path, monkeypatch) -> None:
    args = _start_args(tmp_path)
    captured: dict[str, object] = {}

    def forbidden_plan(_args):
        raise AssertionError("detached start must not plan in the parent process")

    class Registry:
        def start(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                pid=123,
                launch_mechanism="test",
                process_identity=SimpleNamespace(provable=True),
            )

    monkeypatch.setattr(orchestration_cli, "_plan_selection_from_args", forbidden_plan)
    import karox.background_orchestration as background

    monkeypatch.setattr(background, "BackgroundOrchestrationRegistry", Registry)
    assert orchestration_cli._handle_orchestrate(args) == 0
    argv = captured["cli_argv"]
    assert isinstance(argv, list)
    assert argv.count("--delegate-workers") == 1
