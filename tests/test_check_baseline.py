"""Pre-existing failures are not the run's to fix (HARD12, 2026-11-10).

Eleven of twelve fresh-task runs ended ``verification_failed``: the approved
suite was red before any edit (offline sandbox, missing extras, root), and the
gate owed every failure, so the model edited unrelated modules, configuration
and tests -- on pyjanitor that turned a correct fix into two regressions. A
failed ``python -c`` exploration snippet also stayed owed forever.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from karox import check_baseline, cli, completion_gate
from karox.action_execution import CapabilityCoreRuntime
from karox.action_policy import ActionDecisionEngine
from karox.core import CoreCommand
from karox.engine_tool_bodies import _checks_body
from karox.models import AccessProfile, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.risk_engine import RiskEngine
from karox.sessions import SessionStore

PY = sys.executable

LOG_BASE = """\
..F.F
=========================== short test summary info ============================
FAILED tests/test_a.py::test_net - OSError: offline
FAILED tests/test_a.py::test_root[x y] - assert 1
2 failed, 3 passed in 0.12s
"""


# -- pure comparison -----------------------------------------------------------


def test_outcome_parses_failing_ids_and_passed_count() -> None:
    out = check_baseline.outcome(LOG_BASE, 1, False)
    assert out.parsed and out.passed == 3 and out.exit_code == 1
    assert out.failing == {"tests/test_a.py::test_net", "tests/test_a.py::test_root[x y]"}


def test_only_preexisting_failures_are_equivalent() -> None:
    base = check_baseline.outcome(LOG_BASE, 1, False)
    now = check_baseline.outcome(
        "FAILED tests/test_a.py::test_net - OSError\n1 failed, 5 passed in 0.2s\n", 1, False
    )
    info = check_baseline.compare(base, now)
    assert info is not None and info["preexisting_only"] is True and info["new"] == []
    assert "counts as passing" in check_baseline.render({"baseline": info})


@pytest.mark.parametrize(
    "log, code, timed_out",
    [
        # a new failure
        ("FAILED tests/test_a.py::test_net\nFAILED tests/test_b.py::test_new\n1 failed, 3 passed in 1s\n", 1, False),
        # fewer passing tests than before (a crash or a skipped module can hide failures)
        ("FAILED tests/test_a.py::test_net\n1 failed, 2 passed in 1s\n", 1, False),
        # a different exit code (collection interrupted)
        ("FAILED tests/test_a.py::test_net\n1 failed, 3 passed in 1s\n", 2, False),
        # timed out
        ("FAILED tests/test_a.py::test_net\n1 failed, 3 passed in 1s\n", 1, True),
        # unparseable summary
        ("FAILED tests/test_a.py::test_net\n", 1, False),
    ],
)
def test_anything_else_is_not_equivalent(log: str, code: int, timed_out: bool) -> None:
    base = check_baseline.outcome(LOG_BASE, 1, False)
    info = check_baseline.compare(base, check_baseline.outcome(log, code, timed_out))
    assert info is None or info["preexisting_only"] is False


def test_a_failure_without_a_parsed_id_is_never_preexisting() -> None:
    base = check_baseline.outcome(LOG_BASE, 1, False)
    nested = check_baseline.outcome("FAILED tests/test_a.py::test_f[a[0] - b] - assert 0\n1 failed in 1s\n", 1, False)
    assert nested.failing == {"tests/test_a.py::test_f[a[0] - b]"}
    # Two failures counted, one id read: the unread one may be the new one.
    now = check_baseline.outcome(
        "FAILED tests/test_a.py::test_net - OSError\nFAILED <doctest x>\n2 failed, 5 passed in 1s\n", 1, False
    )
    assert now.failed == 2 and len(now.failing) == 1
    info = check_baseline.compare(base, now)
    assert info is not None and info["preexisting_only"] is False


def test_a_doubly_quiet_run_is_read_from_its_progress_rows() -> None:
    quiet_base = "FFF.........                [100%]\n" + "".join(
        f"FAILED tests/test_s.py::test_{n}\n" for n in ("a", "b", "c")
    )
    base = check_baseline.outcome(quiet_base, 1, False)
    assert base.parsed and base.passed == 9 and base.failed == 3 and len(base.failing) == 3
    now = check_baseline.outcome(quiet_base.replace("FFF....", "FFF.....", 1).replace("[100%]", "[ 90%]\n.  [100%]"), 1, False)
    info = check_baseline.compare(base, now)
    assert info is not None and info["preexisting_only"] is True, info
    # An unfinished progress row, or a failure mark with no id, settles nothing.
    assert not check_baseline.outcome("FF..   [ 40%]\nFAILED tests/test_s.py::test_a\n", 1, False).parsed
    two_marks = check_baseline.outcome("FF..  [100%]\nFAILED tests/test_s.py::test_a\n", 1, False)
    info = check_baseline.compare(check_baseline.outcome("F...  [100%]\nFAILED tests/test_s.py::test_a\n", 1, False), two_marks)
    assert info is not None and info["preexisting_only"] is False


def test_new_failures_are_named_for_the_model() -> None:
    base = check_baseline.outcome(LOG_BASE, 1, False)
    now = check_baseline.outcome(
        "FAILED tests/test_a.py::test_net\nFAILED tests/test_b.py::test_new - x\n1 failed, 9 passed in 1s\n",
        1,
        False,
    )
    info = check_baseline.compare(base, now)
    assert info is not None and info["new"] == ["tests/test_b.py::test_new"]
    text = check_baseline.render({"baseline": info})
    assert "1 failure(s) are NEW" in text and "tests/test_b.py::test_new" in text


def test_a_still_failing_test_the_task_names_is_a_target_not_preexisting() -> None:
    """A partial fix of the named tests must not count as passing (audit 2026-11-13)."""

    base = check_baseline.outcome(
        "FAILED t.py::test_a\nFAILED t.py::test_b[x - 1] - e\nFAILED t.py::test_net\n3 failed, 10 passed in 1s\n",
        1,
        False,
    )
    now = check_baseline.outcome(
        "FAILED t.py::test_b[x - 1] - e\nFAILED t.py::test_net\n2 failed, 11 passed in 1s\n", 1, False
    )
    names = check_baseline.task_test_names("Fix the parser so test_a and test_b pass.")
    info = check_baseline.compare(base, now, names)
    assert info is not None and info["preexisting_only"] is False
    assert [n.split("::")[-1][:6] for n in info["task_targets_failing"]] == ["test_b"]
    assert "the task's targets: make them pass" in check_baseline.render({"baseline": info})
    # Without names in the task the old rule stands: only pre-existing reds.
    assert check_baseline.compare(base, now)["preexisting_only"] is True


def test_only_pytest_is_compared() -> None:
    assert check_baseline.is_pytest([PY, "-m", "pytest", "-q", "tests"])
    assert check_baseline.is_pytest(["pytest", "tests"])
    assert not check_baseline.is_pytest([PY, "run.py"])


# -- completion gate -------------------------------------------------------------


class _Core:
    def __init__(self, repository: Path, commands: tuple[tuple[str, ...], ...] = ()) -> None:
        self.repository = repository
        self.verification_commands = frozenset(commands)
        self.verification_command_order = commands

    def safe_path(self, relative: str) -> Path:
        return (self.repository / relative).resolve()

    def tools(self) -> list[object]:
        return []


def _gate_record(tmp_path: Path, checks: list[tuple[list[str], dict[str, object]]], task: str = "Fix VALUE."):
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    record = SimpleNamespace(
        changed_files=["mod.py"], task=task, requirements=[], evidence=[], provider_history=[]
    )
    core = _Core(tmp_path)
    current = completion_gate.identity(core, record)
    for n, (argv, extra) in enumerate(checks):
        ev = f"ev-{n}"
        record.evidence.append({"evidence_id": ev, "metadata": {"completion_identity": current}})
        code = int(extra.get("exit_code", 0))  # type: ignore[arg-type]
        data = {"argv": argv, "exit_code": code, "timed_out": False,
                "verification_eligible": True, "completion_identity": current, **extra}
        record.provider_history.append({
            "role": "tool", "core_name": "checks.run",
            "result": {"ok": code == 0 or extra.get("preexisting_only") is True,
                       "evidence": [{"evidence_id": ev}], "data": data},
        })
    return core, record


def test_a_failed_inline_snippet_is_not_owed(tmp_path: Path) -> None:
    probe = [PY, "-c", "import mod; raise SystemExit(mod.VALUE)"]
    suite = [PY, "-m", "pytest", "-q"]
    core, record = _gate_record(tmp_path, [(probe, {"exit_code": 1}), (suite, {})])
    reasons = completion_gate.floor(core, record)
    assert not any("failed check" in r for r in reasons), reasons


def test_a_failed_approved_command_is_still_owed(tmp_path: Path) -> None:
    suite = [PY, "-m", "pytest", "-q"]
    core, record = _gate_record(tmp_path, [(suite, {"exit_code": 1})])
    reasons = completion_gate.floor(core, record)
    assert any(r.startswith("active failed check:") for r in reasons)


def test_preexisting_only_counts_as_a_passing_check(tmp_path: Path) -> None:
    suite = [PY, "-m", "pytest", "-q"]
    core, record = _gate_record(tmp_path, [(suite, {"exit_code": 1, "preexisting_only": True})])
    reasons = completion_gate.floor(core, record)
    assert not any("failed check" in r for r in reasons), reasons
    assert "missing behavioral evidence" not in reasons


def test_checks_body_puts_the_baseline_note_first() -> None:
    info = {"preexisting": ["t::a"], "new": [], "preexisting_only": True}
    body = _checks_body({"stdout": "1 failed, 2 passed in 1s", "baseline": info})
    assert body.startswith("[karox baseline] All 1 failing tests already failed")


# -- Core end to end -----------------------------------------------------------------


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pkg.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_pkg.py").write_text(
        "import os, sys\nsys.path.insert(0, os.getcwd())\nimport pkg\n\n"
        "def test_env():\n    assert os.environ.get('KAROX_NEVER_SET') == 'x'\n\n"
        "def test_value():\n    assert pkg.value() in (1, 2)\n\n"
        "def test_other():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return repo


SUITE = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]


def _runtime(repository: Path, tmp_path: Path) -> tuple[CapabilityCoreRuntime, Origin]:
    store = SessionStore(tmp_path / "sessions")
    store.create(repository, "fix it", AccessProfile.WORKSPACE_WRITE, session_id="session-1")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    origin = Origin(OriginKind.NATIVE_AGENT, "baseline-test")
    policy.set_grants(origin, cli._agent_grants(AccessProfile.WORKSPACE_WRITE, "build"))
    risk = RiskEngine()
    core = CapabilityCoreRuntime(
        repository, policy, store, tmp_path / "audit.jsonl", risk=risk,
        action_decisions=ActionDecisionEngine(risk), verification_commands=[SUITE],
    )
    return core, origin


def _do(core: CapabilityCoreRuntime, origin: Origin, name: str, arguments: dict, key: str):
    lease = core.sessions.acquire("session-1", "test", ttl_seconds=60.0)
    try:
        return core.execute(
            CoreCommand(name, arguments, "session-1", origin, idempotency_key=key, deadline_seconds=120.0),
            lease=lease,
        )
    finally:
        core.sessions.release(lease)


def test_fixed_command_failures_are_compared_with_the_start_bytes(
    repository: Path, tmp_path: Path
) -> None:
    core, origin = _runtime(repository, tmp_path)
    edit = _do(core, origin, "repo.write_file",
               {"path": "pkg.py", "content": "def value():\n    return 2\n"}, "w1")
    assert edit.ok
    same = _do(core, origin, "checks.run", {"argv": SUITE}, "c1")
    assert same.data["exit_code"] == 1
    assert same.data["preexisting_only"] is True and same.ok
    assert core._check_baselines[tuple(SUITE)].failing == {"tests/test_pkg.py::test_env"}


# -- on-demand baseline (no fixed command; swap changed files to start bytes) --


def _wild_runtime(repository: Path, tmp_path: Path, *extra: list) -> tuple[CapabilityCoreRuntime, Origin]:
    store = SessionStore(tmp_path / "sessions")
    store.create(repository, "fix it", AccessProfile.WORKSPACE_WRITE, session_id="session-1")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    origin = Origin(OriginKind.NATIVE_AGENT, "baseline-test")
    policy.set_grants(origin, cli._agent_grants(AccessProfile.WORKSPACE_WRITE, "build"))
    risk = RiskEngine()
    core = CapabilityCoreRuntime(
        repository, policy, store, tmp_path / "audit.jsonl", risk=risk,
        action_decisions=ActionDecisionEngine(risk),
        verification_commands=[[PY, "-m", "pytest", "*"], *extra],
    )
    return core, origin


def test_on_demand_baseline_restores_every_byte(repository: Path, tmp_path: Path) -> None:
    core, origin = _wild_runtime(repository, tmp_path)
    fixed = "def value():\n    return 2\n"
    _do(core, origin, "repo.write_file", {"path": "pkg.py", "content": fixed}, "w1")
    _do(core, origin, "repo.write_file", {"path": "extra.py", "content": "X = 1\n"}, "w2")

    run = _do(core, origin, "checks.run", {"argv": SUITE}, "c1")
    assert run.data["exit_code"] == 1 and run.data["preexisting_only"] is True and run.ok
    assert (repository / "pkg.py").read_text(encoding="utf-8") == fixed
    assert (repository / "extra.py").read_text(encoding="utf-8") == "X = 1\n"
    assert not check_baseline.swap_journal_path(repository).exists()

    _do(core, origin, "repo.write_file", {"path": "pkg.py", "content": "def value():\n    return 3\n"}, "w3")
    worse = _do(core, origin, "checks.run", {"argv": SUITE}, "c2")
    assert not worse.ok and worse.data["baseline"]["new"] == ["tests/test_pkg.py::test_value"]


def test_a_run_before_the_first_edit_is_the_baseline(repository: Path, tmp_path: Path) -> None:
    core, origin = _wild_runtime(repository, tmp_path)
    first = _do(core, origin, "checks.run", {"argv": SUITE}, "c0")
    assert first.data["exit_code"] == 1 and "baseline" not in first.data
    assert core._check_baselines[tuple(SUITE)].failing == {"tests/test_pkg.py::test_env"}
    _do(core, origin, "repo.write_file", {"path": "pkg.py", "content": "def value():\n    return 2\n"}, "w1")
    same = _do(core, origin, "checks.run", {"argv": SUITE}, "c1")
    assert same.data["preexisting_only"] is True and same.ok


def test_a_crashed_swap_is_restored_from_the_journal(tmp_path: Path) -> None:
    target = tmp_path / "repo" / "pkg.py"
    target.parent.mkdir()
    target.write_text("mine\n", encoding="utf-8")
    journal = check_baseline.swap_journal_path(tmp_path / "repo")
    check_baseline.write_swap_journal(journal, {str(target): b"mine\n", str(target) + ".new": None})
    check_baseline.place({str(target): b"start\n", str(target) + ".new": b"x"})
    assert check_baseline.recover_swap(journal) is True
    assert target.read_bytes() == b"mine\n" and not Path(str(target) + ".new").exists()
    assert not journal.exists()


def test_one_check_cannot_take_the_whole_run(repository: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_MAX_CHECK_SECONDS", "2")
    core, _origin = _wild_runtime(repository, tmp_path)
    plan = core._prepare_check({"argv": SUITE, "timeout_seconds": 3600}, 3600.0)
    assert plan.effective_timeout == 2.0 and "check_cap" in str(plan.clamped_by)
    monkeypatch.delenv("KAROX_MAX_CHECK_SECONDS")
    plan = core._prepare_check({"argv": SUITE, "timeout_seconds": 3600}, 3600.0)
    assert plan.effective_timeout == 300.0


def test_final_fallback_reruns_the_last_passing_check_not_a_bare_suite() -> None:
    from karox.engine_kernel_harness import _last_passing_check

    def check(argv: list[str], ok: bool) -> dict:
        return {"core_name": "checks.run", "result": {"ok": ok, "data": {"argv": argv, "verification_eligible": True}}}

    record = SimpleNamespace(provider_history=[
        check([PY, "-m", "pytest", "-q", "tests/test_a.py"], True),
        check([PY, "-m", "pytest", "-q", "tests/test_b.py"], False),
        check([PY, "-c", "print(1)"], True),
    ])
    assert _last_passing_check(record) == [PY, "-m", "pytest", "-q", "tests/test_a.py"]
    assert _last_passing_check(SimpleNamespace(provider_history=[])) == []


# -- a tool the image lacks is no evidence (HARD12 gha4: 70 failed `-m ruff` runs owed) --


def test_missing_tool_is_recognised_only_when_the_tool_itself_is_absent() -> None:
    absent = {"exit_code": 1, "stderr": "/usr/local/bin/python: No module named ruff"}
    assert check_baseline.missing_tool([PY, "-m", "ruff", "check", "a.py"], absent) == "ruff"
    assert check_baseline.missing_tool(["black", "."], {"exit_code": 127, "stderr": "not found"}) == "black"
    lint = {"exit_code": 1, "stdout": "a.py:1:1: F401 unused import"}
    assert check_baseline.missing_tool([PY, "-m", "ruff", "check", "a.py"], lint) == ""
    inner = {"exit_code": 1, "stdout": "E   ModuleNotFoundError: No module named 'numpy'"}
    assert check_baseline.missing_tool([PY, "-m", "pytest", "t.py"], inner) == ""
    assert check_baseline.missing_tool([PY, "-m", "ruff"], {"exit_code": 0, "stderr": "No module named ruff"}) == ""


def test_a_missing_tool_is_recognised_inside_a_shell_chain() -> None:
    chain = "python -m ruff format --check a.py 2>&1; python -m ruff check a.py 2>&1 | tail -5"
    gone = {"exit_code": 1, "stdout": "/opt/py: No module named ruff\n/opt/py: No module named ruff\n"}
    assert check_baseline.missing_tool(["bash", "-lc", chain], gone) == "ruff"
    assert check_baseline.missing_tool(["bash", "-lc", "cd /app && mypy src"], {"exit_code": 127, "stderr": "bash: mypy: command not found"}) == "mypy"
    # Another command in the chain can still fail on its own: still a check.
    mixed = "python -m ruff check a.py; python -m pytest -q tests"
    assert check_baseline.missing_tool(["bash", "-lc", mixed], gone) == ""
    inner = {"exit_code": 1, "stdout": "E   ModuleNotFoundError: No module named 'numpy'"}
    assert check_baseline.missing_tool(["bash", "-lc", "python -m pytest -q"], inner) == ""
    # A chain that prints the message itself proves nothing about the tool.
    faked = {"exit_code": 1, "stdout": "ruff: command not found\na.py:1:1: F401 unused import"}
    assert check_baseline.missing_tool(["bash", "-lc", "echo 'ruff: command not found'; ruff check ."], faked) == ""


def test_a_missing_tool_check_is_marked_and_not_owed(repository: Path, tmp_path: Path) -> None:
    core, origin = _wild_runtime(repository, tmp_path, [PY, "*"])
    result = _do(core, origin, "checks.run", {"argv": [PY, "-m", "no_such_tool_k5", "check", "."]}, "m1")
    assert result.data["tool_unavailable"] == "no_such_tool_k5"
    assert "do not run or install it again" in result.data["detail"]
    lint, slow = [PY, "-m", "no_such_tool_k5", "check", "."], [PY, "-m", "pytest", "-q", "tests"]
    (tmp_path / "g").mkdir()
    gate_core, record = _gate_record(tmp_path / "g", [(lint, {"exit_code": 1, "tool_unavailable": "no_such_tool_k5"}),
                                                      (slow, {"exit_code": -9, "timed_out": True}),
                                                      ([PY, "-m", "pytest", "-q"], {})])
    assert not any("failed check" in r for r in completion_gate.floor(gate_core, record))


# -- the task is the spec: an existing test pinning the old behaviour (HARD12 gha6) --

_TASK = "Fix it. The marker must come last: output that ends with the marker is what the other path produces."


def _finish(args: dict) -> dict:
    import json as _json
    return {"role": "assistant", "tool_calls": [{"name": "finish", "raw_arguments": _json.dumps(args)}]}


def test_superseded_needs_a_verbatim_task_quote() -> None:
    ok = {"test": "tests/t.py::test_old", "reason": "output that ends with the marker is what the other path produces"}
    bad = {"test": "tests/t.py::test_other", "reason": "I think this test is wrong anyway, honestly"}
    record = SimpleNamespace(task=_TASK, provider_history=[_finish({"answer": "x", "superseded_tests": [ok, bad]})])
    assert check_baseline.superseded(record) == {"tests/t.py::test_old"}
    assert check_baseline.superseded(SimpleNamespace(task=_TASK, provider_history=[])) == frozenset()


def test_only_declared_new_failures_are_discharged() -> None:
    names = frozenset({"test_old"})
    assert check_baseline.only_superseded({"baseline": {"new": ["tests/t.py::test_old"]}}, names)
    assert not check_baseline.only_superseded({"baseline": {"new": ["tests/t.py::test_old", "tests/t.py::x"]}}, names)
    assert not check_baseline.only_superseded({"exit_code": 1}, names)  # no baseline: stays owed


def test_gate_accepts_a_declared_superseded_failure_only(tmp_path: Path) -> None:
    suite = [PY, "-m", "pytest", "-q"]
    info = {"new": ["tests/t.py::test_old"], "preexisting": [], "preexisting_only": False}
    core, record = _gate_record(tmp_path, [(suite, {"exit_code": 1, "baseline": info})], task=_TASK)
    assert any(r.startswith("active failed check:") for r in completion_gate.floor(core, record))
    record.provider_history.append(_finish({"answer": "done", "superseded_tests": [
        {"test": "tests/t.py::test_old", "reason": "The marker must come last"}]}))
    assert not any("failed check" in r for r in completion_gate.floor(core, record))


_PREFIX_LOG = '=================================== FAILURES ===================================\n_______________________________ test_parse_long ________________________________\n\n    def test_parse_long():\n>       assert 1 == 2\nE       assert 1 == 2\n\ntests/test_a.py:10: AssertionError\n__________________________________ test_parse __________________________________\n\n    def test_parse():\n>       assert 3 == 4\nE       assert 3 == 4\n\ntests/test_a.py:20: AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_a.py::test_parse_long - assert 1 == 2\nFAILED tests/test_a.py::test_parse - assert 3 == 4\n2 failed in 0.1s\n'


def test_a_test_whose_name_prefixes_another_gets_its_own_section() -> None:
    """Audit 2026-11-13: test_parse took the line of test_parse_long."""

    from karox.check_parsers import _pytest

    lines = {row["test_id"].split("::")[-1]: row["line"] for row in _pytest(_PREFIX_LOG)}
    assert lines == {"test_parse_long": 10, "test_parse": 20}


def test_a_broken_submodule_import_is_not_a_missing_tool() -> None:
    def missing(argv: list, err: str) -> str:
        return check_baseline.missing_tool(argv, {"exit_code": 1, "stderr": err})

    assert missing(["python", "-m", "mypkg.cli"], "No module named 'mypkg.utils'") == ""
    assert missing(["python", "-m", "ruff", "check"], "No module named ruff") == "ruff"
    assert missing(["python", "-m", "mypkg.cli"], "No module named 'mypkg'") == "mypkg"


def test_a_wrapped_and_escaped_task_quote_still_counts() -> None:
    """click-3239 (v50): the model wrapped the quote in its own words and
    JSON-escaped the task's inner quotes; both attempts were refused."""

    task = 'Intro. Here, "disabled" means that the flag is *not* passed, which means the `default=True` value is used.'
    wrapped = {
        "test": "tests/t.py::test_old",
        "reason": 'The task specifies: "Here, \\"disabled\\" means that the flag is *not* passed, which means the '
        '`default=True` value is used." This case expects the old special-case behaviour.',
    }
    partial = {"test": "tests/t.py::test_part", "reason": 'Per the task, "the flag is not passed, which means the default" holds.'}
    record = SimpleNamespace(task=task, provider_history=[_finish({"answer": "x", "superseded_tests": [wrapped, partial]})])
    assert check_baseline.superseded(record) == {"tests/t.py::test_old", "tests/t.py::test_part"}


def test_coloured_pytest_output_still_parses() -> None:
    """pygmt duel1: --color=yes hid every summary row, so no baseline was taken."""

    esc = chr(27)
    text = (
        f"{esc}[31mFAILED{esc}[0m pygmt/tests/test_x.py::test_a - OSError\n"
        f"{esc}[31m===== {esc}[31m{esc}[1m1 failed{esc}[0m, {esc}[32m8 passed{esc}[0m{esc}[31m in 2.10s{esc}[0m{esc}[31m ====={esc}[0m\n"
    )
    result = check_baseline.outcome(text, 1, False)
    assert result.parsed and result.passed == 8
    assert result.failing == {"pygmt/tests/test_x.py::test_a"}


def test_a_node_id_rerun_of_old_failures_counts_as_preexisting() -> None:
    start = check_baseline.Outcome(1, False, frozenset({"t.py::test_a"}), 0, True)
    now = check_baseline.Outcome(1, False, frozenset({"t.py::test_a"}), 0, True)
    assert check_baseline.compare(start, now)["preexisting_only"] is True
