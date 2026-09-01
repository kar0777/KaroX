from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from karox.intelligence_pool import IntelligenceEndpoint
from karox.mission_control import AgentStatus, MissionSnapshot
from karox import orchestration_cli
from karox.orchestration_presenter import render_mission, render_started


def _snapshot() -> MissionSnapshot:
    return MissionSnapshot(
        run_id="run-1",
        task_id="task-1",
        objective="Implement authentication",
        recipe="feature",
        orchestrator_endpoint_id="api:openai:orch",
        status="running",
        agents=(
            AgentStatus(
                step_id="implement",
                role="implementer",
                endpoint_id="sub:codex",
                status="running",
                activity="editing guarded files",
                evidence_count=2,
            ),
        ),
        actual_cost_usd=1.25,
        total_tokens=987654,
        context_reused_chars=4000000,
        cache_hit_rate=0.89,
    )


def test_started_output_is_task_first_and_hides_process_internals() -> None:
    text = render_started(
        {
            "status": "started",
            "run_id": "run-1",
            "pid": 1234,
            "launch_mechanism": "windows-detached",
            "identity_provable": True,
            "mission_command": "karox mission-control show run-1",
        },
        objective="Implement authentication",
    )
    assert text.splitlines()[0] == "KaroX started"
    assert "Implement authentication" in text
    assert "Track: karox mission run-1" in text
    assert "1234" not in text
    assert "windows-detached" not in text


def test_started_details_restore_process_diagnostics() -> None:
    text = render_started(
        {
            "run_id": "run-1",
            "pid": 1234,
            "launch_mechanism": "windows-detached",
            "identity_provable": True,
        },
        details=True,
    )
    assert "Run ID: run-1" in text
    assert "PID: 1234" in text
    assert "Launch: windows-detached" in text
    assert "Process identity: verified" in text


def test_mission_default_is_progress_first_and_hides_raw_telemetry() -> None:
    text = render_mission(
        _snapshot().to_dict(),
        endpoint_names={
            "api:openai:orch": "Strong Orchestrator",
            "sub:codex": "Codex subscription",
        },
    )
    assert "Implement authentication" in text
    assert "Status: running · Progress 0%" in text
    assert "Orchestrator: Strong Orchestrator" in text
    assert "Implementer → Codex subscription" in text
    assert "editing guarded files" in text
    assert "Incremental cost: $1.2500" in text
    assert "Cache: 89%" in text
    assert "987654" not in text
    assert "4000000" not in text
    assert "api:openai:orch" not in text
    assert "sub:codex" not in text


def test_mission_details_restore_runtime_identity() -> None:
    text = render_mission(_snapshot().to_dict(), details=True)
    assert "Run ID: run-1" in text
    assert "Task ID: task-1" in text
    assert "Tokens: 987654" in text
    assert "Context reused: 4000000 chars" in text
    assert "api:openai:orch" in text
    assert "sub:codex" in text
    assert "evidence: 2" in text


def test_mission_cli_show_uses_human_presenter_by_default(capsys) -> None:
    store = Mock()
    store.snapshot.return_value = _snapshot()
    pool = Mock()
    pool.list.return_value = [
        IntelligenceEndpoint(
            endpoint_id="api:openai:orch",
            display_name="Strong Orchestrator",
            source_kind="api",
            provider_id="openai",
            model_id="orch",
            roles=("orchestrator",),
        ),
        IntelligenceEndpoint(
            endpoint_id="sub:codex",
            display_name="Codex subscription",
            source_kind="subscription",
            target_id="codex",
            roles=("implementer",),
            already_paid=True,
        ),
    ]
    args = argparse.Namespace(
        run_id="run-1", mission_control_command="show", json=False, details=False
    )
    with (
        patch("karox.orchestration_cli.MissionControlStore", return_value=store),
        patch("karox.orchestration_cli.IntelligencePool", return_value=pool),
    ):
        assert orchestration_cli._handle_mission_control(args) == 0
    text = capsys.readouterr().out
    assert "Strong Orchestrator" in text
    assert "Codex subscription" in text
    assert "sub:codex" not in text
    assert "987654" not in text


def test_start_cli_uses_compact_human_output(capsys, tmp_path: Path) -> None:
    record = SimpleNamespace(
        pid=4321,
        launch_mechanism="test-detached",
        process_identity=SimpleNamespace(provable=True),
    )
    registry = Mock()
    registry.start.return_value = record
    args = argparse.Namespace(
        orchestrate_command="start",
        repository=tmp_path,
        objective="Implement authentication",
        recipe="feature",
        preset="balanced",
        risk="medium",
        orchestrator=None,
        delegate_workers=False,
        run_id=None,
        label="",
        assign=[],
        worker_effort=[],
        verification_command=[],
        max_steps=96,
        max_seconds=3600.0,
        isolate_implementers=False,
        baseline_cost=None,
        json=False,
    )
    with patch(
        "karox.background_orchestration.BackgroundOrchestrationRegistry",
        return_value=registry,
    ):
        assert orchestration_cli._handle_orchestrate(args) == 0
    text = capsys.readouterr().out
    assert "KaroX started" in text
    assert "Implement authentication" in text
    assert f"Track: karox mission {args.run_id}" in text
    assert "4321" not in text
    assert "test-detached" not in text
