from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from karox import background_orchestration as bg
from karox.mission_control import MissionControlStore, MissionSnapshot
from karox.process_identity import ProcessIdentity, ProcessIdentityVerdict


def test_background_start_uses_private_request_file_not_raw_objective_in_process_argv(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = bg.BackgroundOrchestrationRegistry(root=tmp_path / "runs")
    captured: dict[str, object] = {}

    def fake_spawn(argv, *, cwd=None):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        return SimpleNamespace(pid=4321), "test-detached"

    monkeypatch.setattr(bg, "spawn_detached", fake_spawn)
    monkeypatch.setattr(
        bg,
        "capture_process_identity",
        lambda pid, **_kw: ProcessIdentity(pid=pid, create_time_ns=123456789),
    )
    record = registry.start(
        run_id="run-bg-test",
        repository=repo,
        cli_argv=[
            "orchestrate",
            "run",
            "--objective",
            "private objective text",
            "--run-id",
            "run-bg-test",
        ],
        recipe="feature",
        preset="balanced",
        label="background test",
    )
    assert record.pid == 4321
    process_argv = captured["argv"]
    assert isinstance(process_argv, list)
    rendered = " ".join(str(item) for item in process_argv)
    assert "private objective text" not in rendered
    assert "karox.background_worker" in rendered
    request = Path(str(process_argv[-1]))
    payload = json.loads(request.read_text(encoding="utf-8"))
    assert payload["argv"][-1] == "run-bg-test"
    assert "private objective text" in payload["argv"]
    persisted = registry.path("run-bg-test").read_text(encoding="utf-8")
    assert "--objective" not in persisted
    assert "private objective text" not in persisted


def test_background_view_uses_process_identity_and_mission_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runs"
    registry = bg.BackgroundOrchestrationRegistry(root=root)
    record = bg.BackgroundRunRecord(
        run_id="run-view",
        repository=str(tmp_path),
        pid=99,
        process_identity=ProcessIdentity(pid=99, create_time_ns=500),
        started_at=1.0,
        launch_mechanism="test",
        label="demo",
    )
    registry.put(record)
    monkeypatch.setattr(
        bg,
        "verify_process_identity",
        lambda *_a, **_k: ProcessIdentityVerdict(True, True, "verified"),
    )
    mission_path = tmp_path / "mission.json"
    store = MissionControlStore("run-view", path=mission_path)
    store.update_snapshot(
        MissionSnapshot(
            run_id="run-view",
            task_id="task",
            objective="demo",
            recipe="feature",
            orchestrator_endpoint_id="sub:orchestrator",
            status="running",
            agents=(),
            updated_at=2.0,
        )
    )
    monkeypatch.setattr(
        bg,
        "MissionControlStore",
        lambda _run_id: MissionControlStore("run-view", path=mission_path),
    )
    view = registry.view("run-view")
    assert view.alive is True
    assert view.identity_proven is True
    assert view.status == "running"
    assert view.snapshot is not None and view.snapshot.objective == "demo"


def test_background_control_refuses_unknown_run_and_enqueues_known_run(
    tmp_path: Path, monkeypatch
) -> None:
    registry = bg.BackgroundOrchestrationRegistry(root=tmp_path / "runs")
    registry.put(
        bg.BackgroundRunRecord(
            run_id="known",
            repository=str(tmp_path),
            pid=7,
            process_identity=ProcessIdentity(pid=7, create_time_ns=1),
            started_at=1.0,
            launch_mechanism="test",
        )
    )
    mission_path = tmp_path / "mission.json"
    monkeypatch.setattr(
        bg,
        "verify_process_identity",
        lambda *_a, **_k: ProcessIdentityVerdict(True, True, "verified"),
    )
    monkeypatch.setattr(
        bg,
        "MissionControlStore",
        lambda _run_id: MissionControlStore("known", path=mission_path),
    )
    command = registry.request("known", "pause")
    assert command["command_type"] == "pause"
    assert MissionControlStore("known", path=mission_path).pending_commands()[0].command_type == "pause"
    try:
        registry.request("missing", "stop")
    except bg.BackgroundOrchestrationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown run was accepted")


def test_background_launch_claim_blocks_duplicate_after_spawn_identity_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = bg.BackgroundOrchestrationRegistry(root=tmp_path / "runs")
    spawn_calls = 0

    def fake_spawn(_argv, *, cwd=None):
        nonlocal spawn_calls
        spawn_calls += 1
        return SimpleNamespace(pid=4242), "test-detached"

    monkeypatch.setattr(bg, "spawn_detached", fake_spawn)

    def fail_identity(*_args, **_kwargs):
        raise RuntimeError("identity capture failed")

    monkeypatch.setattr(bg, "capture_process_identity", fail_identity)
    try:
        registry.start(
            run_id="claimed-run",
            repository=repo,
            cli_argv=["orchestrate", "run"],
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("identity failure was not propagated")

    assert (registry.root / ".launch-claimed-run.claim").exists()
    try:
        registry.start(
            run_id="claimed-run",
            repository=repo,
            cli_argv=["orchestrate", "run"],
        )
    except bg.BackgroundOrchestrationError as exc:
        assert "reserved" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate launch was accepted")
    assert spawn_calls == 1


def test_mission_control_command_id_is_retry_safe(tmp_path: Path) -> None:
    store = MissionControlStore("retry-safe", path=tmp_path / "mission.json")
    first = store.enqueue("pause", command_id="cmd-retry-safe")
    second = store.enqueue("pause", command_id="cmd-retry-safe")

    assert second.command_id == first.command_id
    assert len(store.pending_commands()) == 1
    try:
        store.enqueue("resume", command_id="cmd-retry-safe")
    except Exception as exc:
        assert "reused for different input" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("conflicting command id was accepted")
