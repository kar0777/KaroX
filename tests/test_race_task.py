"""Behavior contracts for the reusable identical-task race runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "race_task.py"
SECRET = "race-test-secret-value"


def run_script(
    *arguments: object, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["RACE_TEST_API_KEY"] = SECRET
    environment["PYTHONIOENCODING"] = "utf-8"
    for name, value in (extra_env or {}).items():
        environment[name] = value
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(item) for item in arguments)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def fake_cli(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    path = tmp_path / "fake_karox.py"
    path.write_text(
        """import json
import os
from pathlib import Path
import sys
import time

if '--check' in sys.argv:
    print('independent check passed')
    raise SystemExit(0)
if '--sleep' in sys.argv:
    time.sleep(5)
capture = os.environ.get('RACE_CAPTURE_ARGV')
if capture:
    Path(capture).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
secret = os.environ.get('RACE_TEST_API_KEY', '')
print(json.dumps({'type': 'event', 'kind': 'step_started', 'step': 1}))
print(json.dumps({'type': 'result', 'report': {
    'session_id': 'fake-session', 'status': 'submitted', 'verified': True,
    'engine': {'name': 'karo-core', 'version': '0.5.1+karo'},
    'usage': {'input_tokens': 12, 'output_tokens': 5, 'total_tokens': 17,
              'costs': {'USD': 0.25}},
}}))
print('captured secret: ' + secret, file=sys.stderr)
""",
        encoding="utf-8",
    )
    return path


def run_fake(tmp_path: Path, *, independent_check: bool = True) -> tuple[subprocess.CompletedProcess[str], Path]:
    agent = fake_cli(tmp_path)
    repository = tmp_path / "repository"
    output = tmp_path / "evidence"
    capture = tmp_path / "argv.json"
    os.environ["RACE_CAPTURE_ARGV"] = str(capture)
    arguments: list[object] = [
        "run",
        "--label",
        "b-test",
        "--repository",
        repository,
        "--task",
        "create hello.txt",
        "--output-dir",
        output,
        "--session-id",
        "b-test-session",
        "--command-json",
        json.dumps([sys.executable, str(agent)]),
        "--model",
        "dsv41f-bench-karo",
        "--base-url",
        "http://127.0.0.1:4000/v1",
        "--api-key-env",
        "RACE_TEST_API_KEY",
        "--max-steps",
        7,
        "--max-seconds",
        11,
        "--max-output-tokens",
        99,
        "--context-window",
        1234,
        "--verification-command",
        '["python", "-m", "pytest", "-q"]',
    ]
    if independent_check:
        arguments.extend(
            ["--independent-check-json", json.dumps([sys.executable, str(agent), "--check"])]
        )
    completed = run_script(*arguments)
    return completed, output


def test_runner_captures_real_process_metrics_and_redacts_logs(tmp_path: Path) -> None:
    completed, output = run_fake(tmp_path)

    assert completed.returncode == 0, completed.stderr
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["event_counts"] == {"step_started": 1}
    assert metrics["usage"]["total_tokens"] == 17
    assert metrics["engine"] == {"name": "karo-core", "version": "0.5.1+karo"}
    assert metrics["agent_verified"] is True
    assert metrics["independently_verified"] is True
    assert metrics["task_passed"] is True
    argv = json.loads(Path(os.environ["RACE_CAPTURE_ARGV"]).read_text(encoding="utf-8"))
    assert argv[:2] == ["agent", "run"]
    assert "--stream-json" in argv
    expected = {
        "--max-steps": "7",
        "--max-seconds": "11",
        "--max-output-tokens": "99",
        "--context-window": "1234",
        "--model": "dsv41f-bench-karo",
        "--base-url": "http://127.0.0.1:4000/v1",
        "--api-key-env": "RACE_TEST_API_KEY",
    }
    for flag, value in expected.items():
        assert argv[argv.index(flag) + 1] == value
    for path in output.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8")


def test_runner_does_not_turn_agent_self_report_into_task_success(tmp_path: Path) -> None:
    completed, output = run_fake(tmp_path, independent_check=False)

    assert completed.returncode == 0, completed.stderr
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["agent_verified"] is True
    assert metrics["independently_verified"] is None
    assert metrics["task_passed"] is None


def test_runner_marks_a_timed_out_process_without_claiming_success(tmp_path: Path) -> None:
    agent = fake_cli(tmp_path)
    output = tmp_path / "evidence"
    completed = run_script(
        "run",
        "--label",
        "timeout",
        "--repository",
        tmp_path / "repository",
        "--task",
        "wait",
        "--output-dir",
        output,
        "--command-json",
        json.dumps([sys.executable, str(agent), "--sleep"]),
        "--timeout-seconds",
        0.2,
    )

    assert completed.returncode == 124
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["timed_out"] is True
    assert metrics["exit_code"] == 124
    assert metrics["task_passed"] is None


def test_runner_refuses_to_contaminate_the_measured_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    output = repository / "evidence"

    completed = run_script(
        "run",
        "--label",
        "bad-output",
        "--repository",
        repository,
        "--task",
        "measure",
        "--output-dir",
        output,
    )

    assert completed.returncode == 2
    assert "outside the repository" in completed.stderr
    assert not output.exists()


def metric(*, max_steps: int, wall: float, tokens: int) -> dict:
    return {
        "label": "run",
        "run_spec": {"task": "same task", "max_steps": max_steps},
        "wall_seconds": wall,
        "usage": {"total_tokens": tokens},
        "agent_verified": True,
        "independently_verified": True,
        "task_passed": True,
    }


def test_compare_reports_metric_deltas_for_identical_run_specs(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    output = tmp_path / "comparison.json"
    first.write_text(json.dumps(metric(max_steps=7, wall=10.0, tokens=100)), encoding="utf-8")
    second.write_text(json.dumps(metric(max_steps=7, wall=14.0, tokens=130)), encoding="utf-8")

    completed = run_script("compare", first, second, "--output", output)

    assert completed.returncode == 0, completed.stderr
    comparison = json.loads(output.read_text(encoding="utf-8"))
    assert comparison["fair"] is True
    assert comparison["delta"]["wall_seconds"] == 4.0
    assert comparison["delta"]["total_tokens"] == 30


def test_compare_rejects_different_agent_budgets(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    output = tmp_path / "comparison.json"
    first.write_text(json.dumps(metric(max_steps=7, wall=10.0, tokens=100)), encoding="utf-8")
    second.write_text(json.dumps(metric(max_steps=8, wall=14.0, tokens=130)), encoding="utf-8")

    completed = run_script("compare", first, second, "--output", output)

    assert completed.returncode == 1
    comparison = json.loads(output.read_text(encoding="utf-8"))
    assert comparison["fair"] is False
    assert comparison["mismatches"] == ["max_steps"]


@pytest.mark.parametrize("value", ["not-json", "[]", '["python", 3]'])
def test_runner_rejects_malformed_process_commands(tmp_path: Path, value: str) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    completed = run_script(
        "run",
        "--label",
        "bad-command",
        "--repository",
        repository,
        "--task",
        "measure",
        "--output-dir",
        tmp_path / "evidence",
        "--command-json",
        value,
    )

    assert completed.returncode == 2
    assert "command JSON" in completed.stderr


def _race_task_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("race_task_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_command_is_allowed_only_for_this_checkouts_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _race_task_module()

    # This test process resolves karox from this repository's src/ (conftest
    # puts it first), so the default contender command is this checkout's.
    command = module._default_command()

    assert command == [sys.executable, "-m", "karox.cli"]


def test_default_command_refuses_an_ambient_karox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib.util

    module = _race_task_module()
    foreign = tmp_path / "site-packages" / "karox" / "__init__.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("", encoding="utf-8")

    def fake_find_spec(name: str, *args: object, **kwargs: object):
        if name != "karox":
            return importlib.util.find_spec(name, *args, **kwargs)
        return importlib.util.spec_from_file_location("karox", str(foreign))

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    with pytest.raises(ValueError, match="command-json"):
        module._default_command()


def test_default_command_refuses_when_no_karox_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    module = _race_task_module()

    def fake_find_spec(name: str, *args: object, **kwargs: object):
        if name != "karox":
            return importlib.util.find_spec(name, *args, **kwargs)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    with pytest.raises(ValueError, match="command-json"):
        module._default_command()


def test_runner_without_command_json_refuses_an_ambient_karox(
    tmp_path: Path,
) -> None:
    # Reproduces the real fairness hazard: an ambient karox install (here a
    # fake package on PYTHONPATH outside this repository) would otherwise be
    # benchmarked instead of the contender.
    repository = tmp_path / "repository"
    repository.mkdir()
    ambient = tmp_path / "ambient"
    (ambient / "karox").mkdir(parents=True)
    (ambient / "karox" / "__init__.py").write_text("", encoding="utf-8")

    completed = run_script(
        "run",
        "--label",
        "ambient",
        "--repository",
        repository,
        "--task",
        "measure",
        "--output-dir",
        tmp_path / "evidence",
        extra_env={"PYTHONPATH": str(ambient)},
    )

    assert completed.returncode == 2
    assert "refusing the default contender command" in completed.stderr
    assert "--command-json" in completed.stderr
    assert not (tmp_path / "evidence" / "metrics.json").exists()
