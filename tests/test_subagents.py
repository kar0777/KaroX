"""General-purpose karo-core workers: budgets, cancellation, isolation, evidence.

A worker is a real engine run: the same vendored karo-core loop, models and
environments the product ``agent run`` path uses, wrapped with per-worker model
context, a capability-scoped command runner, its own workspace, hard budgets,
cooperative cancellation and a structured result the parent can reconcile.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

import pytest

from karox.core_engine import CoreEngine, CoreEngineConfig
from karox.engine import __version__ as engine_version
from karox.engine.agents.default import DefaultAgent
from karox.engine.models.test_models import DeterministicModel, make_output
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.subagents import (
    CancellationToken,
    SubagentManager,
    WorkerAgent,
    WorkerBudgets,
    WorkerCommandPolicy,
    WorkerEngine,
    WorkerKind,
    WorkerTask,
    reconcile,
)


class FakeModel:
    """Replays scripted messages; records the history it was asked to send."""

    def __init__(self, messages: List[dict]):
        self._messages = messages
        self.seen: List[List[dict]] = []
        self.calls = 0

    def query(self, messages: List[dict], **kwargs) -> dict:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        index = min(self.calls - 1, len(self._messages) - 1)
        return self._messages[index]

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def action(command: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "extra": {"actions": [{"command": command}], "cost": 0.0},
    }


SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def manager_for(
    tmp_path: Path,
    *,
    profile: AccessProfile = AccessProfile.WORKSPACE_WRITE,
    parent_grants: Optional[List[Capability]] = None,
    max_workers: int = 4,
) -> SubagentManager:
    policy = CapabilityPolicy(profile)
    parent = Origin(OriginKind.NATIVE_AGENT, "cli-parent")
    if parent_grants is not None:
        policy.set_grants(parent, parent_grants)
    return SubagentManager(
        policy,
        session_id="race-1",
        root=tmp_path / "workers",
        parent_origin=parent if parent_grants is not None else None,
        max_workers=max_workers,
    )


def task_for(
    tmp_path: Path,
    worker_id: str,
    *,
    kind: WorkerKind = WorkerKind.GENERAL,
    messages: Optional[List[dict]] = None,
    budgets: Optional[WorkerBudgets] = None,
    workspace_mode: str = "scratch",
    input_files: tuple = (),
    repository: Optional[Path] = None,
    cancellation: Optional[CancellationToken] = None,
    verifies: Optional[str] = None,
) -> WorkerTask:
    scripted = (
        messages
        if messages is not None
        else [
            action('python -c "open(\'out.txt\',\'w\').write(\'done\')"'),
            action(SUBMIT),
        ]
    )
    return WorkerTask(
        worker_id=worker_id,
        kind=kind,
        task=f"task for {worker_id}",
        repository=repository or tmp_path,
        workspace_mode=workspace_mode,
        input_files=tuple(input_files),
        model_factory=lambda config: FakeModel(scripted),
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        budgets=budgets or WorkerBudgets(),
        cancellation=cancellation,
        verifies=verifies,
    )


# ------------------------------------------------------------- engine identity


def test_worker_classes_build_on_the_vendored_engine() -> None:
    assert issubclass(WorkerEngine, CoreEngine)
    assert issubclass(WorkerAgent, DefaultAgent)


def test_worker_run_reports_the_engine_version(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    result = manager.run([task_for(tmp_path, "w-engine")])[0]
    assert result.status == "submitted", result.submission
    assert result.engine_version == engine_version


# -------------------------------------------------------------- basics + events


def test_worker_submits_and_reports_artifacts_events_and_trajectory(
    tmp_path: Path,
) -> None:
    manager = manager_for(tmp_path)
    result = manager.run([task_for(tmp_path, "w-basic")])[0]
    assert result.status == "submitted"
    assert result.workspace and Path(result.workspace).is_dir()
    assert (Path(result.workspace) / "out.txt").read_text(encoding="utf-8") == "done"
    artifacts = {item["path"]: item for item in result.artifacts}
    assert "out.txt" in artifacts
    assert len(artifacts["out.txt"]["sha256"]) == 64
    events = [
        json.loads(line)
        for line in Path(result.events_path).read_text(encoding="utf-8").splitlines()
    ]
    types = [item["type"] for item in events]
    assert types[0] == "worker.started"
    assert "worker.model_call" in types
    assert "worker.command" in types
    assert types[-1] == "worker.exit"
    assert result.trajectory_path and Path(result.trajectory_path).is_file()
    assert result.steps >= 2
    assert result.evidence


def test_model_context_is_isolated_per_worker(tmp_path: Path) -> None:
    models_a: List[FakeModel] = []
    models_b: List[FakeModel] = []

    def factory_a(config: CoreEngineConfig) -> FakeModel:
        model = FakeModel([action(SUBMIT)])
        models_a.append(model)
        return model

    def factory_b(config: CoreEngineConfig) -> FakeModel:
        model = FakeModel([action(SUBMIT)])
        models_b.append(model)
        return model

    tasks = [
        WorkerTask(
            "w-ctx-a", WorkerKind.GENERAL, "alpha investigation", tmp_path,
            model_factory=factory_a, model="m",
        ),
        WorkerTask(
            "w-ctx-b", WorkerKind.GENERAL, "beta investigation", tmp_path,
            model_factory=factory_b, model="m",
        ),
    ]
    manager = manager_for(tmp_path)
    results = manager.run(tasks)
    assert all(result.status == "submitted" for result in results)
    assert len(models_a) == 1 and len(models_b) == 1
    history_a = "\n".join(str(m.get("content", "")) for m in models_a[0].seen[-1])
    history_b = "\n".join(str(m.get("content", "")) for m in models_b[0].seen[-1])
    assert "alpha investigation" in history_a
    assert "beta investigation" not in history_a
    assert "beta investigation" in history_b


# ---------------------------------------------------------------------- budgets


def test_step_budget_is_enforced(tmp_path: Path) -> None:
    messages = [action(f"echo step-{i}") for i in range(10)]
    task = task_for(
        tmp_path,
        "w-steps",
        messages=messages,
        budgets=WorkerBudgets(max_steps=2, max_seconds=60),
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "steps_exceeded"
    assert result.steps == 2


def test_wall_time_budget_is_enforced(tmp_path: Path) -> None:
    slow = DeterministicModel(
        outputs=[
            make_output("", [{"command": "/sleep 5"}]),
            make_output("", [{"command": "echo late"}]),
        ]
    )
    task = WorkerTask(
        "w-wall", WorkerKind.GENERAL, "slow task", tmp_path,
        model_factory=lambda config: slow, model="m",
        budgets=WorkerBudgets(max_steps=8, max_seconds=1, command_timeout=30),
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "wall_time_exceeded"


def test_token_budget_is_enforced(tmp_path: Path) -> None:
    big = "x" * 2_000
    messages = [action(f"echo {big}-{i}") for i in range(6)]
    task = task_for(
        tmp_path,
        "w-tokens",
        messages=messages,
        budgets=WorkerBudgets(max_steps=6, max_seconds=60, max_tokens=200),
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "token_budget_exceeded"
    assert result.tokens > 0


def test_budgets_are_validated() -> None:
    with pytest.raises(ValueError):
        WorkerBudgets(max_steps=0)
    with pytest.raises(ValueError):
        WorkerBudgets(max_seconds=0)
    with pytest.raises(ValueError):
        WorkerBudgets(max_tokens=-1)
    with pytest.raises(ValueError):
        WorkerBudgets(command_timeout=0)
    with pytest.raises(ValueError):
        WorkerBudgets(max_steps=999)


# ------------------------------------------------------------------ cancellation


def test_cancellation_between_steps_stops_the_run(tmp_path: Path) -> None:
    token = CancellationToken()

    class CancellingModel(FakeModel):
        def query(self, messages: List[dict], **kwargs) -> dict:
            # The operator cancels while the first model call is in flight;
            # the next query must observe it before spending another call.
            token.cancel("operator requested")
            return super().query(messages, **kwargs)

    model = CancellingModel([action(f"echo work-{i}") for i in range(6)])
    task = WorkerTask(
        "w-cancel", WorkerKind.GENERAL, "cancel me", tmp_path,
        model_factory=lambda config: model, model="m",
        cancellation=token,
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "cancelled"
    assert result.cancellation_reason == "operator requested"
    assert model.calls == 1


def test_cancellation_before_start_skips_the_model(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel("upfront decision")
    model = FakeModel([action(SUBMIT)])
    task = WorkerTask(
        "w-pre", WorkerKind.GENERAL, "never starts", tmp_path,
        model_factory=lambda config: model, model="m",
        cancellation=token,
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "cancelled"
    assert model.calls == 0


def test_cancel_all_stops_running_workers(tmp_path: Path) -> None:
    slow = DeterministicModel(
        outputs=[
            make_output("", [{"command": "/sleep 5"}]),
            make_output("", [{"command": "echo still-working"}]),
            make_output("", [{"command": SUBMIT}]),
        ]
    )
    tasks = [
        WorkerTask(
            "w-slow", WorkerKind.GENERAL, "slow worker", tmp_path,
            model_factory=lambda config: slow, model="m",
            budgets=WorkerBudgets(max_steps=8, max_seconds=120),
        ),
        task_for(tmp_path, "w-fast"),
    ]
    manager = manager_for(tmp_path, max_workers=2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.run, tasks)
        deadline = time.time() + 15
        while time.time() < deadline:
            fast_events = manager.root / "w-fast" / "events.ndjson"
            slow_events = manager.root / "w-slow" / "events.ndjson"
            if (
                slow_events.exists()
                and fast_events.exists()
                and "worker.exit" in fast_events.read_text(encoding="utf-8")
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail("workers did not start in time")
        manager.cancel_all("operator stop")
        results = future.result(timeout=60)

    by_id = {result.worker_id: result for result in results}
    assert by_id["w-slow"].status == "cancelled"
    assert by_id["w-slow"].cancellation_reason == "operator stop"
    assert by_id["w-fast"].status == "submitted"


# --------------------------------------------------------- command policy gates


def test_read_only_worker_denies_mutating_commands(tmp_path: Path) -> None:
    repo = tmp_path / "site"
    repo.mkdir()
    messages = [
        action('python -c "open(\'x.txt\',\'w\').write(\'pwned\')"'),
        action(SUBMIT),
    ]
    task = WorkerTask(
        "w-research", WorkerKind.RESEARCH, "read the code", repo,
        workspace_mode="repository",
        model_factory=lambda config: FakeModel(messages), model="m",
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "submitted"
    assert result.denials and result.denials[0]["stage"] == "filter"
    assert not (repo / "x.txt").exists()


def test_command_policy_segments_and_confines(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    read_only = WorkerCommandPolicy(WorkerKind.RESEARCH)
    write = WorkerCommandPolicy(WorkerKind.GENERAL)

    assert read_only.allow("cat notes.md", workspace)[0] is True
    assert read_only.allow("git status", workspace)[0] is True
    assert read_only.allow("git log --oneline", workspace)[0] is True
    assert read_only.allow("git push origin main", workspace)[0] is False
    assert read_only.allow("python -c \"open('x','w')\"", workspace)[0] is False
    assert read_only.allow("cat notes.md && rm -rf .", workspace)[0] is False

    assert write.allow("python -c \"open('x','w')\"", workspace)[0] is True
    assert write.allow("cd sub && python build.py", workspace)[0] is True
    assert write.allow("cd .. && cat ../secret", workspace)[0] is False
    assert write.allow("ssh host.example", workspace)[0] is False
    assert write.allow("git push origin main", workspace)[0] is False
    assert write.allow("echo hi | grep hi", workspace)[0] is True


def test_ungranted_worker_records_capability_denials(tmp_path: Path) -> None:
    messages = [action("echo denied-step"), action(SUBMIT)]
    task = WorkerTask(
        "w-caps", WorkerKind.GENERAL, "needs grants", tmp_path,
        model_factory=lambda config: FakeModel(messages), model="m",
    )
    manager = manager_for(tmp_path, parent_grants=[Capability.REPO_READ])
    result = manager.run([task])[0]
    assert result.denials
    assert result.denials[0]["stage"] == "capability"
    assert "process.run" in result.denials[0]["reason"]


def test_manager_grants_intersect_the_parent_origin(tmp_path: Path) -> None:
    manager = manager_for(
        tmp_path, parent_grants=[Capability.REPO_READ, Capability.REPO_WRITE]
    )
    tasks = [
        WorkerTask(
            "w-grant-code", WorkerKind.CODING, "code", tmp_path,
            model_factory=lambda c: FakeModel([action(SUBMIT)]), model="m",
        ),
        WorkerTask(
            "w-grant-research", WorkerKind.RESEARCH, "read", tmp_path,
            model_factory=lambda c: FakeModel([action(SUBMIT)]), model="m",
        ),
    ]
    manager.run(tasks)
    grants = {
        key: caps
        for key, caps in manager.policy.origin_grants.items()
        if key.startswith("subagent:")
    }
    assert grants["subagent:w-grant-code"] == {
        Capability.REPO_READ,
        Capability.REPO_WRITE,
    }
    assert grants["subagent:w-grant-research"] == {Capability.REPO_READ}


# ------------------------------------------------------------- workspace modes


def test_copy_mode_isolates_the_repository(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    messages = [
        action(
            'python -c "open(\'src/app.py\',\'a\').write(\'patched\\n\');'
            "open('out.txt','w').write('1')\""
        ),
        action(SUBMIT),
    ]
    task = WorkerTask(
        "w-copy", WorkerKind.CODING, "patch the app", repo,
        workspace_mode="copy", input_files=("src/app.py",),
        model_factory=lambda config: FakeModel(messages), model="m",
    )
    manager = manager_for(tmp_path)
    result = manager.run([task])[0]
    assert result.status == "submitted"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "print('app')\n"
    assert not (repo / "out.txt").exists()
    workspace = Path(result.workspace)
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == (
        "print('app')\npatched\n"
    )
    assert (workspace / "out.txt").exists()
    assert {item["path"] for item in result.artifacts} >= {"src/app.py", "out.txt"}


def test_scratch_mode_rejects_input_files(tmp_path: Path) -> None:
    task = WorkerTask(
        "w-bad", WorkerKind.GENERAL, "bad task", tmp_path,
        workspace_mode="scratch", input_files=("notes.md",),
        model_factory=lambda c: FakeModel([action(SUBMIT)]), model="m",
    )
    manager = manager_for(tmp_path)
    with pytest.raises(ValueError, match="input_files"):
        manager.run([task])


def test_input_files_are_confined_and_bounded(tmp_path: Path) -> None:
    task = WorkerTask(
        "w-escape", WorkerKind.GENERAL, "escape", tmp_path,
        workspace_mode="copy", input_files=("../outside.txt",),
        model_factory=lambda c: FakeModel([action(SUBMIT)]), model="m",
    )
    manager = manager_for(tmp_path)
    with pytest.raises(ValueError, match="input_files"):
        manager.run([task])


# ------------------------------------------------------------------ concurrency


def test_concurrent_workers_do_not_cross_contaminate(tmp_path: Path) -> None:
    def make(index: int) -> WorkerTask:
        messages = [
            action(f'python -c "open(\'only-{index}.txt\',\'w\').write(\'{index}\')"'),
            action(SUBMIT),
        ]
        return WorkerTask(
            f"w-conc-{index}", WorkerKind.GENERAL, f"worker {index}", tmp_path,
            model_factory=lambda config, m=messages: FakeModel(m), model="m",
        )

    tasks = [make(index) for index in range(3)]
    manager = manager_for(tmp_path, max_workers=2)
    results = manager.run(tasks)
    assert [result.worker_id for result in results] == [
        task.worker_id for task in tasks
    ]
    assert all(result.status == "submitted" for result in results)
    for index, result in enumerate(results):
        workspace = Path(result.workspace)
        assert (workspace / f"only-{index}.txt").exists()
        siblings = sorted(item.name for item in workspace.iterdir() if item.is_file())
        assert siblings == [f"only-{index}.txt"]
    manifest = json.loads(
        (manager.root / "manifest.json").read_text(encoding="utf-8")
    )
    assert [item["worker_id"] for item in manifest["workers"]] == [
        task.worker_id for task in tasks
    ]
    assert all(item["status"] == "submitted" for item in manifest["workers"])


# --------------------------------------------------------------- reconciliation


def verification_task(
    tmp_path: Path, worker_id: str, command: str, verifies: Optional[str]
) -> WorkerTask:
    messages = [action(command), action(SUBMIT)]
    return WorkerTask(
        worker_id, WorkerKind.VERIFICATION, "verify the change", tmp_path,
        workspace_mode="scratch",
        model_factory=lambda config: FakeModel(messages), model="m",
        verifies=verifies,
    )


def test_reconciliation_verified_when_checks_pass(tmp_path: Path) -> None:
    coding = task_for(tmp_path, "w-code-1")
    verify = verification_task(
        tmp_path, "w-verify-1", 'python -c "print(\'checks ok\')"', verifies="w-code-1"
    )
    manager = manager_for(tmp_path)
    results = manager.run([coding, verify])
    report = manager.reconcile(results)
    assert report.overall == "verified"
    assert not report.conflicts
    verdicts = {item.worker_id: item.verdict for item in report.workers}
    assert verdicts["w-verify-1"] == "pass"
    assert verdicts["w-code-1"] is None
    assert report.evidence
    json.dumps(report.to_dict())


def test_reconciliation_conflict_when_verification_fails(tmp_path: Path) -> None:
    coding = task_for(tmp_path, "w-code-2")
    verify = verification_task(
        tmp_path, "w-verify-2", 'python -c "import sys; sys.exit(3)"', verifies="w-code-2"
    )
    manager = manager_for(tmp_path)
    results = manager.run([coding, verify])
    report = manager.reconcile(results)
    assert report.overall == "failed"
    assert report.conflicts
    conflict = report.conflicts[0]
    assert conflict["verifier"] == "w-verify-2"
    assert conflict["worker_id"] == "w-code-2"
    verdicts = {item.worker_id: item.verdict for item in report.workers}
    assert verdicts["w-verify-2"] == "fail"


def test_reconciliation_without_verification_is_incomplete(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    results = manager.run([task_for(tmp_path, "w-solo")])
    report = manager.reconcile(results)
    assert report.overall == "incomplete"


def test_reconciliation_function_is_importable() -> None:
    assert callable(reconcile)


# -------------------------------------------------------------- task validation


def test_task_validation() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        WorkerTask("Bad Id!", WorkerKind.GENERAL, "t", Path("."))
    with pytest.raises(ValueError, match="task"):
        WorkerTask("ok-id", WorkerKind.GENERAL, "   ", Path("."))
    with pytest.raises(ValueError, match="workspace_mode"):
        WorkerTask(
            "ok-id", WorkerKind.GENERAL, "t", Path("."),
            workspace_mode="network",
        )
    with pytest.raises(ValueError, match="verifies"):
        WorkerTask("ok-id", WorkerKind.GENERAL, "t", Path("."), verifies="ok-id")
