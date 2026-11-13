"""Offline scenarios replayed from real benchmark failures.

No model is called: a scripted provider plays the model's side of each run and
the real ``karox agent run`` executes every tool. Each scenario pins one thing
a paid run once tripped over, so a regression shows up here instead of after a
benchmark run.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any, List, Sequence

import pytest

from test_engine_kernel import (
    ScriptedProvider,
    _finish_call,
    _git,
    _parse_stream,
    _request_text,
    _request_tools,
    _response,
    _tool_call,
)

CALC = '''def div(a, b):
    return a / b
'''

#: The model-facing surface of a plain repair. Top CLIs ship 6-10 tools; every
#: extra schema is paid on every request (66 trajectories: the tools outside
#: this set made 1.4% of calls). Hidden tools stay callable.
PLAIN_SURFACE = {
    "checks_run",
    "finish",
    "git_diff",
    "git_log",
    "repo_edit_file",
    "repo_list_files",
    "repo_read_lines",
    "repo_search",
    "repo_write_file",
    "skill_load",
    "web_fetch",
}


def _repo(tmp_path: Path, *, git: bool = True) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "calc.py").write_text(CALC, encoding="utf-8")
    if git:
        _git(repository, "init", "-q")
        _git(repository, "config", "user.email", "t@example.invalid")
        _git(repository, "config", "user.name", "T")
        _git(repository, "add", "-A")
        _git(repository, "commit", "-q", "-m", "seed")
    return repository


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    calls: Sequence[Any],
    *,
    task: str = "div(a, 0) must raise ZeroDivisionError('b must not be zero')",
    commands: Sequence[Sequence[str]] = (),
    finishes: int = 4,
) -> tuple[int, dict, ScriptedProvider]:
    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    responses = [_response((call,), content=f"step {index}") for index, call in enumerate(calls)]
    responses += [_response((_finish_call(f"f{index}", "div raises on zero"),)) for index in range(finishes)]
    provider = ScriptedProvider(responses)
    monkeypatch.setattr("karox.cli_runtime.OpenAIChatCompletionsProvider", lambda *a, **k: provider)
    arguments: List[str] = [
        "agent", "run", "--repository", str(repository), "--task", task,
        "--model", "scripted-model", "--base-url", "http://127.0.0.1:9/v1",
        "--session-id", "scenario-1", "--max-steps", "30", "--max-seconds", "300",
        "--verification-command", json.dumps([sys.executable, "*"]),
        "--stream-json",
    ]
    for command in commands:
        arguments += ["--verification-command", json.dumps(list(command))]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(arguments)
    _, report = _parse_stream(output.getvalue())
    return code, report, provider


def _fix_call(call_id: str) -> Any:
    return _tool_call(call_id, "repo_edit_file", {
        "path": "calc.py",
        "old_string": "    return a / b\n",
        "new_string": "    if b == 0:\n        raise ZeroDivisionError('b must not be zero')\n    return a / b\n",
    })


def _probe_call(call_id: str) -> Any:
    script = (
        "from calc import div\n"
        "for a in (1, -7, 0):\n"
        "    try:\n"
        "        div(a, 0)\n"
        "        raise SystemExit('no error for %r' % a)\n"
        "    except ZeroDivisionError as exc:\n"
        "        assert str(exc) == 'b must not be zero', exc\n"
        "print('probe ok', div(6, 3))\n"
    )
    return _tool_call(call_id, "checks_run", {"argv": [sys.executable, "-c", script]})


def test_a_plain_repair_pays_for_a_small_tool_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), [_fix_call("c1"), _probe_call("c2")])
    assert code == 0, report
    advertised = _request_tools(provider.requests[0])
    assert advertised == PLAIN_SURFACE
    assert report["verified"] is True


def test_hidden_tools_stay_callable_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    (repository / "notes.txt").write_text("untracked-marker\n", encoding="utf-8")
    calls = [
        _tool_call("c1", "repo_read_file", {"path": "calc.py"}),
        _tool_call("c2", "git_status", {}),
        _fix_call("c3"),
        _probe_call("c4"),
    ]
    code, report, provider = _run(tmp_path, monkeypatch, repository, calls)
    assert code == 0, report
    assert "return a / b" in _request_text(provider.requests[1])
    assert "notes.txt" in _request_text(provider.requests[2])
    assert report["verified"] is True


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_an_approved_shell_rule_runs_its_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """swe/tb 2026-11-12: bash -lc * was approved yet every script was refused."""

    calls = [_tool_call("c1", "checks_run", {"argv": ["bash", "-lc", "echo probe-$((2+3)) && ls"]})]
    _code, _report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls,
        commands=[["bash", "-lc", "*"]], task="report the number the probe prints",
    )
    observed = _request_text(provider.requests[1])
    assert "probe-5" in observed
    assert "calc.py" in observed


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_a_failed_heredoc_probe_then_a_passing_one_lets_finish_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB 4.1 v48: a heredoc probe was not seen as an inline probe, so its
    first failure stayed an open failed check and finish kept bouncing."""

    python = Path(sys.executable).as_posix()
    heredoc = (
        f"{python} - <<'EOF'\nfrom calc import div\ntry:\n    div(1, 0)\nexcept ZeroDivisionError as exc:\n"
        "    assert str(exc) == 'b must not be zero', exc\nprint('heredoc ok')\nEOF"
    )
    calls = [
        _tool_call("c1", "checks_run", {"argv": ["bash", "-lc", heredoc]}),
        _fix_call("c2"),
        _tool_call("c3", "checks_run", {"argv": ["bash", "-lc", heredoc]}),
    ]
    code, report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls, commands=[["bash", "-lc", "*"]], finishes=2,
    )
    assert "heredoc ok" in _request_text(provider.requests[3])
    assert code == 0, report
    assert report["verified"] is True


def test_the_third_identical_call_names_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeepSeek Harness repeat reminder: Flash re-reads the same window."""

    read = {"path": "calc.py", "start": 1, "count": 5}
    calls = [_tool_call(f"r{index}", "repo_read_lines", read) for index in range(3)]
    calls += [_fix_call("c1"), _probe_call("c2")]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    assert "exact repo.read_lines call" not in _request_text(provider.requests[2])
    assert "exact repo.read_lines call 3 times" in _request_text(provider.requests[3])


def test_a_timed_out_check_says_how_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [_tool_call("c1", "checks_run", {
        "argv": [sys.executable, "-c", "import time; time.sleep(20)"], "timeout_seconds": 1,
    })]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls, task="run the slow script")
    observed = _request_text(provider.requests[1])
    assert "timed out after" in observed
    assert "timeout_seconds" in observed


def test_a_repository_without_git_finishes_without_a_review_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB 4.1 v47: no git meant no change review could ever be produced, and
    finish bounced on that missing review until the run timed out."""

    code, report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path, git=False), [_fix_call("c1"), _probe_call("c2")], finishes=3,
    )
    finish_requests = len(provider.requests) - 2
    assert code == 0, report
    assert finish_requests <= 2, report
    assert report["verified"] is True, report.get("reason")
