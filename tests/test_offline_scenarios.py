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
    "code_run",
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
    extra: Sequence[str] = (),
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
    arguments += list(extra)
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


def test_page_guidance_rides_only_on_requests_that_carry_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A library repair used to read three paragraphs about pages and
    # requirement IDs; a page task still gets them.
    (tmp_path / "plain").mkdir()
    (tmp_path / "page").mkdir()
    _, _, plain = _run(tmp_path / "plain", monkeypatch, _repo(tmp_path / "plain"), [_fix_call("c1")])
    assert "browser_command `open`" not in _request_text(plain.requests[0])
    _, _, page = _run(
        tmp_path / "page", monkeypatch, _repo(tmp_path / "page"), [_fix_call("c1")],
        task="make the start button on the html page call div(a, 0) safely",
    )
    assert "browser_command" in _request_tools(page.requests[0])
    assert "browser_command `open`" in _request_text(page.requests[0])


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


def test_a_read_only_directory_still_lets_writable_files_be_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB 4.1 risk-scorer (v48 and the cloud replay): /app is root-owned and
    the agent user cannot create the sibling temporary an atomic write needs,
    while the files inside stay writable."""

    import tempfile

    from karox import atomic_io

    target = tmp_path / "mod.py"
    target.write_bytes(b"VALUE = 1\n")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", refuse)
    atomic_io.atomic_write(target, b"VALUE = 22\n")
    assert target.read_bytes() == b"VALUE = 22\n"
    with pytest.raises(PermissionError, match="not writable by this process"):
        atomic_io.atomic_write(tmp_path / "new.py", b"x = 1\n")


def test_a_long_command_streak_after_an_edit_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB 4.1 risk-scorer v50: 129 commands, 9 edits, wall-time limit."""

    calls: List[Any] = [_fix_call("c1")]
    calls += [_tool_call(f"p{index}", "checks_run", {"argv": [sys.executable, "-c", f"print({index})"]}) for index in range(12)]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    assert "commands since your last code change" not in _request_text(provider.requests[12])
    assert "12 commands since your last code change" in _request_text(provider.requests[13])


def _run_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands: Sequence[Sequence[str]], seconds: int) -> ScriptedProvider:
    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    responses = [_response((_fix_call("c1"),)), _response((_probe_call("c2"),))]
    responses += [_response((_finish_call(f"f{index}", "div raises on zero"),)) for index in range(5)]
    provider = ScriptedProvider(responses)
    monkeypatch.setattr("karox.cli_runtime.OpenAIChatCompletionsProvider", lambda *a, **k: provider)
    arguments = [
        "agent", "run", "--repository", str(_repo(tmp_path)), "--task", "div(a, 0) must raise ZeroDivisionError",
        "--model", "scripted-model", "--base-url", "http://127.0.0.1:9/v1", "--session-id", "deep-1",
        "--max-steps", "30", "--max-seconds", str(seconds), "--stream-json",
    ]
    for command in commands:
        arguments += ["--verification-command", json.dumps(list(command))]
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(arguments)
    return provider


def test_an_early_probe_only_finish_gets_one_adversarial_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB 4.1 v50: mvcc finished verified after 5 of 59 minutes and failed."""

    provider = _run_budget(tmp_path, monkeypatch, [[sys.executable, "*"]], 3600)
    assert _request_text(provider.requests[-1]).count("one adversarial pass (asked only once)") == 1


def test_a_run_with_an_approved_test_suite_still_gets_the_adversarial_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rebench26 v60h: existing tests stayed green on a fix missing a combination."""

    provider = _run_budget(tmp_path, monkeypatch, [[sys.executable, "-m", "pytest", "*"], [sys.executable, "*"]], 3600)
    assert _request_text(provider.requests[-1]).count("one adversarial pass (asked only once)") == 1


def test_a_short_budget_skips_the_adversarial_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _run_budget(tmp_path, monkeypatch, [[sys.executable, "*"]], 600)
    assert "adversarial pass" not in _request_text(provider.requests[-1])


def test_rerunning_a_probe_after_an_edit_is_not_called_a_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scim2 duel1: an edited probe re-run was told its result would not change."""

    read = {"path": "calc.py", "start": 1, "count": 5}
    calls = [_tool_call("r0", "repo_read_lines", read), _tool_call("r1", "repo_read_lines", read), _fix_call("c1"),
             _tool_call("r2", "repo_read_lines", read), _probe_call("c2")]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    assert all("exact repo.read_lines call" not in _request_text(request) for request in provider.requests)


def _green_call(call_id: str) -> Any:
    return _tool_call(call_id, "checks_run", {"argv": [sys.executable, "-c", "print('pytest: 3 passed')"]})


def _settle_seen(provider: ScriptedProvider) -> bool:
    return any("responses ago and nothing has been edited" in _request_text(request) for request in provider.requests)


def test_a_green_suite_before_any_edit_does_not_say_finish_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exstruct-113 v52: a passing baseline before any edit was taken as "done"."""

    read = {"path": "calc.py", "start": 1, "count": 2}
    calls = [_green_call("g1")] + [_tool_call(f"r{i}", "repo_read_lines", dict(read, count=2 + i)) for i in range(5)]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    assert not _settle_seen(provider)


def test_a_green_suite_after_an_edit_still_says_finish_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = {"path": "calc.py", "start": 1, "count": 2}
    calls = [_fix_call("c1"), _green_call("g1")] + [
        _tool_call(f"r{i}", "repo_read_lines", dict(read, count=2 + i)) for i in range(5)
    ]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    assert _settle_seen(provider)


UNCHANGED_PASS = "finishing without changing any file, but the task asks for a change"


def test_a_change_task_finishing_unchanged_gets_one_measurement_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exstruct-113 v52: "already implemented", unchanged tree, verified answer."""

    calls = [_tool_call("r1", "repo_read_lines", {"path": "calc.py"})]
    _code, _report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls, task="Optimize div so the module imports faster"
    )
    assert _request_text(provider.requests[-1]).count(UNCHANGED_PASS) == 1


def test_a_question_finishing_unchanged_gets_no_measurement_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [_tool_call("r1", "repo_read_lines", {"path": "calc.py"})]
    _code, _report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls, task="What does div return for 6 and 3?"
    )
    assert all(UNCHANGED_PASS not in _request_text(request) for request in provider.requests)


def test_a_one_line_question_with_a_change_word_is_not_a_change_task() -> None:
    from karox.engine_tool_support import task_expects_change

    assert not task_expects_change("What versions does this library support?")
    assert not task_expects_change("Зачем функция reduce вызывает update дважды?")
    assert task_expects_change("Can you fix the parser?")
    assert task_expects_change("Would it be possible to fix the parser?")
    assert task_expects_change("How do I fix the parser?")
    assert task_expects_change("Как исправить ошибку? Исправь её?")
    assert task_expects_change("What is wrong here? Please fix it?")
    assert task_expects_change("Fix the parser so empty input works")
    assert task_expects_change("Why does it crash?\nPlease fix it so the bug is gone.")
    assert not task_expects_change("Summarise the architecture")


def test_a_change_task_with_an_edit_gets_no_measurement_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), [_fix_call("c1"), _probe_call("c2")])
    assert all(UNCHANGED_PASS not in _request_text(request) for request in provider.requests)


def test_the_review_request_is_not_followed_by_finish_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wtforms-892 duel1: "finish now" landed right after the review request."""

    read = {"path": "calc.py", "start": 1, "count": 2}
    calls = [_fix_call("c1"), _green_call("g1"), _finish_call("f-early", "div raises on zero")] + [
        _tool_call(f"r{i}", "repo_read_lines", dict(read, count=2 + i)) for i in range(5)
    ]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls, extra=["--effort-level", "high"])
    assert any("review the change once" in _request_text(request) for request in provider.requests)
    assert not _settle_seen(provider)


def test_no_review_is_started_on_the_last_allowed_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review begun with no model call left ended a submitted run on step_limit."""

    calls = [_fix_call("c1"), _green_call("g1")]
    _code, report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls, extra=["--effort-level", "high", "--max-steps", "3"]
    )
    assert all("review the change once" not in _request_text(request) for request in provider.requests)
    assert "step_limit" not in json.dumps(report)


def test_the_review_names_old_raises_in_the_edited_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """datamodel-3070 v54: the review item about old raises was dismissed unprobed."""

    repository = _repo(tmp_path)
    (repository / "calc.py").write_text(
        "def div(a, b):\n    if a is None:\n        raise TypeError('a is required')\n    return a / b\n", encoding="utf-8"
    )
    _git(repository, "commit", "-qam", "guard")
    calls = [_fix_call("c1"), _probe_call("c2")]
    _code, _report, provider = _run(tmp_path, monkeypatch, repository, calls, extra=["--effort-level", "high"])
    observed = "".join(_request_text(request) for request in provider.requests)
    assert "calc.py:3 `if a is None: raise TypeError('a is required')`" in observed


GOAL_REFUSAL = 'list each goal the task states in finish "goals"'


def test_after_the_review_a_finish_without_goal_evidence_is_refused_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v55 exstruct/wtforms: the agent's own output showed the goal unmet and it finished."""

    calls = [_fix_call("c1"), _probe_call("c2")]
    code, report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls, extra=["--effort-level", "high"])
    observed = "".join(_request_text(request) for request in provider.requests)
    assert observed.count(GOAL_REFUSAL) == 1
    assert report["verified"] is True, report.get("reason")


def test_goals_quoting_fresh_output_pass_without_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goals = [{"goal": "div(6, 3) still divides", "observed": "probe ok 2.0"}]
    calls = [_fix_call("c1"), _probe_call("c2"), _finish_call("f0", "done"),
             _tool_call("f1", "finish", {"answer": "done", "goals": goals})]
    _code, report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls, extra=["--effort-level", "high"])
    assert all(GOAL_REFUSAL not in _request_text(request) for request in provider.requests)


def test_a_run_without_the_review_is_never_asked_for_goals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), [_fix_call("c1"), _probe_call("c2")])
    assert all(GOAL_REFUSAL not in _request_text(request) for request in provider.requests)


def test_prompt_and_review_stay_concise_and_name_repo_measures() -> None:
    from karox.agent import SYSTEM_PROMPT
    from karox.prompts import self_review_text

    review = self_review_text()
    assert len(SYSTEM_PROMPT) < 7500
    assert len(review) < 3000
    # v58: compaction dropped the direct-form and named-module rules and two
    # tasks regressed; they stay, stated as principles rather than examples.
    assert "in the form its users would reach for first" in SYSTEM_PROMPT
    assert "add its direct form" in review
    assert "never a reason to skip it" in SYSTEM_PROMPT
    assert "Measure the way the repository measures" in SYSTEM_PROMPT
    assert "what existing callers of that interface expect" in SYSTEM_PROMPT
    assert "never add a new error for a combination nothing forbids" in SYSTEM_PROMPT
    assert "the way existing tests check sibling code" in review


def test_prompt_and_review_carry_no_examples_lifted_from_benchmark_tasks() -> None:
    """Rules came from analysed tasks; their examples named those tasks' fixes.

    An example such as "a dict like sibling dump methods" or "sys.modules
    submodules" tunes every run toward one task's hidden test. The principle
    stays; the task's own answer does not.
    """

    from karox.agent import SYSTEM_PROMPT
    from karox.engine_prompts import _ENGINE_CONTRACT
    from karox.prompts import self_review_text

    text = (SYSTEM_PROMPT + _ENGINE_CONTRACT + self_review_text()).lower()
    for fragment in (
        "sys.modules",
        "dump method",
        "render time",
        "constructor",
        "leftover header",
        "module still loaded",
        "partial mapping",
        "pre-encoded",
    ):
        assert fragment not in text, fragment


def test_failing_probe_after_green_test_clears_settle_and_avoids_review_punt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = {"path": "calc.py", "start": 1, "count": 2}
    failing_probe = _tool_call(
        "p_fail",
        "checks_run",
        {"argv": [sys.executable, "-c", "print('still broken'); raise SystemExit(1)"]},
    )
    calls = [_fix_call("c1"), _green_call("g1"), failing_probe] + [
        _tool_call(f"r{i}", "repo_read_lines", dict(read, count=2 + i)) for i in range(5)
    ]
    _code, _report, provider = _run(tmp_path, monkeypatch, _repo(tmp_path), calls)
    observed = "".join(_request_text(request) for request in provider.requests)
    assert not _settle_seen(provider)
    assert "finish review will name anything missing" not in observed
    assert "finish review names anything missing" not in observed


def test_goal_ledger_rejects_quotes_copied_from_a_failing_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_probe = _tool_call(
        "p_fail",
        "checks_run",
        {"argv": [sys.executable, "-c", "print('probe mismatch output'); raise SystemExit(1)"]},
    )
    bad_goals = [{"goal": "div raises on zero", "observed": "probe mismatch output"}]
    good_goals = [{"goal": "div raises on zero", "observed": "probe ok 2.0"}]
    calls = [
        _fix_call("c1"),
        failing_probe,
        _finish_call("f0", "done"),
        _tool_call("f1", "finish", {"answer": "done", "goals": bad_goals}),
        _probe_call("c2"),
        _tool_call("f2", "finish", {"answer": "done", "goals": good_goals}),
    ]
    code, report, provider = _run(
        tmp_path, monkeypatch, _repo(tmp_path), calls, extra=["--effort-level", "high"]
    )
    last_request = _request_text(provider.requests[-1])
    assert "These goals quote no output received after your last edit: div raises on zero" in last_request
    assert last_request.count(GOAL_REFUSAL) == 1
    assert code == 0, report
    assert report["verified"] is True, report.get("reason")
