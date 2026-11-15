"""A pass discharges a coverage-only pytest failure, never one it cannot vouch for."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from karox import completion_gate
from karox.pytest_selection import covered, measures_coverage, selection
from test_check_baseline import PY, _gate_record

ROOT = "/testbed"


def test_reporting_and_coverage_options_are_understood() -> None:
    failed = selection(["python", "-m", "pytest", "tests/test_x.py", "-q", "--cov=pkg.mod", "--cov-report", "term"])
    assert failed == frozenset({"tests/test_x.py"})
    plain = selection(["python3", "-m", "pytest", "/testbed/tests/test_x.py", "-rA", "-p", "no:cacheprovider"], ROOT)
    assert covered(failed, [plain])
    wrapped = ["python", "-m", "coverage", "run", "--source=pkg", "-m", "pytest", "tests/test_x.py", "-q"]
    assert selection(wrapped) == frozenset({"tests/test_x.py"}) and measures_coverage(wrapped)
    assert selection(["coverage", "run", "-m", "pytest", "./tests/"]) == frozenset({"tests"})


def test_any_option_that_can_change_the_outcome_is_not_understood() -> None:
    for extra in (
        ["-k", "ok"], ["-qk", "ok"], ["-kok"], ["-m", "fast"], ["-o", "addopts="], ["-W", "error"],
        ["-n", "2"], ["--import-mode=importlib"], ["--strict-markers"], ["--runslow"], ["--skip-slow"],
        ["--deselect", "tests/test_x.py::test_a"], ["-p", "no:faulthandler"], ["--co"], ["--lf"],
        ["--cov-fail-under=100"], ["--", "-q"], ["-c", "other.ini"],
    ):
        assert selection(["pytest", "tests/test_x.py", *extra], ROOT) is None, extra
    assert selection(["pytest", "../elsewhere/test_x.py"], ROOT) is None
    assert selection(["pytest", "/other/test_x.py"], ROOT) is None


def test_a_pass_must_select_every_failed_target() -> None:
    failed = selection(["pytest", "tests/test_x.py", "tests/test_y.py"])
    assert not covered(failed, [selection(["pytest", "tests/test_x.py"])])
    assert covered(failed, [selection(["pytest", "tests"])])
    assert covered(selection(["pytest", "tests/test_x.py::test_a"]), [selection(["pytest", "tests/test_x.py"])])
    assert not covered(selection(["pytest", "tests"]), [selection(["pytest", "tests/test_x.py"])])
    assert covered(selection(["pytest", "tests"]), [selection(["pytest", "-q"])])  # whole suite


def test_only_the_plain_shell_form_at_the_root_is_read() -> None:
    chain = "cd /testbed && COVERAGE_CORE=ctrace python -m coverage run --source=pkg -m pytest tests/test_x.py -q && python -m coverage report -m"
    assert selection(["bash", "-lc", chain], ROOT) == frozenset({"tests/test_x.py"})
    for script in (
        "cd /testbed/sub && pytest tests/test_x.py",
        "cd /testbed && FEATURE=on pytest tests/test_x.py",
        "cd /testbed && pytest tests/test_x.py | tail -5",
        "cd /testbed && pytest a.py && pytest b.py",
        "cd /testbed && pytest 'tests/test_x.py::test_a[1]'",
        "pytest tests; true",
    ):
        assert selection(["bash", "-lc", script], ROOT) is None, script
    assert selection(["make", "test"]) is None


def test_the_gate_drops_a_cov_failure_the_plain_run_passes(tmp_path: Path) -> None:
    cov = [PY, "-m", "pytest", "tests/test_mod.py", "-q", "--cov=mod"]
    plain = [PY, "-m", "pytest", "tests/test_mod.py", "-q"]
    core, record = _gate_record(tmp_path, [(cov, {"exit_code": 2}), (plain, {})])
    assert not any("failed check" in reason for reason in completion_gate.floor(core, record))


def test_the_gate_keeps_a_failure_no_understood_pass_answers(tmp_path: Path) -> None:
    strict = [PY, "-m", "pytest", "tests/test_mod.py", "-q", "-W", "error"]
    plain = [PY, "-m", "pytest", "tests/test_mod.py", "-q"]
    narrowed = [PY, "-m", "pytest", "tests/test_mod.py", "-q", "-k", "not slow"]
    core, record = _gate_record(tmp_path, [(strict, {"exit_code": 1}), (plain, {})])
    assert any(reason.startswith("active failed check:") for reason in completion_gate.floor(core, record))
    (tmp_path / "n").mkdir()
    core, record = _gate_record(tmp_path / "n", [(plain, {"exit_code": 1}), (narrowed, {})])
    assert any(reason.startswith("active failed check:") for reason in completion_gate.floor(core, record))


def test_the_repair_summary_offers_the_plain_run_only_for_a_coverage_failure(tmp_path: Path) -> None:
    from karox.engine_kernel_harness import OPTION_FAILURE_HINT, EngineHarnessMixin

    cov = [PY, "-m", "pytest", "tests/test_mod.py", "-q", "--cov=mod"]
    core, record = _gate_record(tmp_path, [(cov, {"exit_code": 2})])
    summary = EngineHarnessMixin._failure_summary(SimpleNamespace(core=core), record)  # type: ignore[arg-type]
    assert "active failed check" in summary and OPTION_FAILURE_HINT in summary
    assert "missing current change review" not in summary
    (tmp_path / "p").mkdir()
    plain = [PY, "-m", "pytest", "tests/test_mod.py", "-q"]
    core, record = _gate_record(tmp_path / "p", [(plain, {"exit_code": 1})])
    summary = EngineHarnessMixin._failure_summary(SimpleNamespace(core=core), record)  # type: ignore[arg-type]
    assert OPTION_FAILURE_HINT not in summary


def test_the_rerun_hint_follows_only_the_latest_failed_check(tmp_path: Path) -> None:
    from karox.engine_kernel_harness import EngineHarnessMixin

    old = {"failures": [{"test_id": "tests/test_old.py::test_a", "path": "tests/test_old.py"}]}
    first = [PY, "-m", "pytest", "tests/test_old.py", "-q"]
    second = [PY, "-m", "pytest", "-q"]
    core, record = _gate_record(tmp_path, [(first, {"exit_code": 1, "failure_map": old}), (second, {"exit_code": 1})])
    summary = EngineHarnessMixin._failure_summary(SimpleNamespace(core=core), record)  # type: ignore[arg-type]
    assert "active failed check" in summary and "Targeted rerun" not in summary, summary
    (tmp_path / "x").mkdir()
    core, record = _gate_record(tmp_path / "x", [(first, {"exit_code": 1, "failure_map": old})])
    summary = EngineHarnessMixin._failure_summary(SimpleNamespace(core=core), record)  # type: ignore[arg-type]
    assert 'targets=["tests/test_old.py::test_a"]' in summary, summary


def test_asking_which_tools_exist_is_never_owed(tmp_path: Path) -> None:
    from karox.completion_gate import _is_tool_query

    query = ["bash", "-lc", "which ruff && ruff --version; which mypy && mypy --version"]
    assert _is_tool_query(query)
    assert _is_tool_query(["bash", "-lc", "cd /testbed && command -v black 2>/dev/null || echo missing"])
    assert _is_tool_query(["python", "-m", "pytest", "--version"])
    assert not _is_tool_query(["bash", "-lc", "ruff check . && ruff --version"])
    assert not _is_tool_query(["bash", "-lc", "which python && python -m pytest tests -q"])
    core, record = _gate_record(tmp_path, [(query, {"exit_code": 1})])
    assert not any("failed check" in reason for reason in completion_gate.floor(core, record))


def test_a_deleted_probe_in_a_shell_chain_is_not_owed(tmp_path: Path) -> None:
    from karox.completion_gate import _scratch_scripts

    chain = ["bash", "-lc", "cp probe.py sub/probe.py && python sub/probe.py"]
    inside = ["bash", "-lc", "cd sub && python -u probe_two.py 2>/dev/null | tail -5"]
    assert _scratch_scripts(chain) == ["probe.py", "sub/probe.py"]
    assert _scratch_scripts(inside) == ["sub/probe_two.py"]
    assert _scratch_scripts(["node", "check.mjs"]) == ["check.mjs"]
    assert _scratch_scripts(["bash", "-lc", "cd a && cd b && python check.py"]) == ["a/b/check.py"]
    assert _scratch_scripts(["bash", "-lc", "cd $HOME && python check.py"]) == []
    assert _scratch_scripts([PY, "-m", "pytest", "tests/test_gone.py"]) == []
    # A probe beside a real check never discharges the check.
    assert _scratch_scripts(["bash", "-lc", "python probe.py && python -m pytest tests -q"]) == []
    assert _scratch_scripts(["bash", "-lc", "python probe.py; make test"]) == []
    (tmp_path / "sub").mkdir()
    core, record = _gate_record(tmp_path, [(chain, {"exit_code": 1}), (inside, {"exit_code": 1})])
    assert not any("failed check" in reason for reason in completion_gate.floor(core, record))
    (tmp_path / "sub" / "probe_two.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    reasons = completion_gate.floor(core, record)
    assert any("failed check" in reason and "probe_two.py" in reason for reason in reasons), reasons


def test_removing_scratch_and_looking_is_never_owed(tmp_path: Path) -> None:
    from karox.completion_gate import _is_cleanup

    cleanup = ["bash", "-lc", "rm -f probe_cdi_scratch.py && ls probe_cdi_scratch.py 2>&1"]
    assert _is_cleanup(cleanup)
    assert _is_cleanup(["bash", "-lc", "rm -f a.py b.py; git status --porcelain"])
    assert not _is_cleanup(["bash", "-lc", "ls probe.py"])  # looking without removing
    assert not _is_cleanup(["bash", "-lc", "rm -f probe.py && python -m pytest tests -q"])
    assert not _is_cleanup(["bash", "-lc", "rm -rf build && make"])
    # Looking at anything but what was removed is a check of its own.
    assert not _is_cleanup(["bash", "-lc", "rm -f probe.py && test -f required-output"])
    assert not _is_cleanup(["bash", "-lc", 'rm -f probe.py; echo "$(python -m pytest -q)"'])
    assert _is_cleanup(["bash", "-lc", "rm -f probe.py && [ ! -e probe.py ] && echo gone"])
    core, record = _gate_record(tmp_path, [(cleanup, {"exit_code": 2})])
    assert not any("failed check" in reason for reason in completion_gate.floor(core, record))


def test_a_tracked_script_or_a_test_target_stays_owed(tmp_path: Path) -> None:
    import subprocess

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*git, "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    (tmp_path / "tools" / "check.py").unlink()
    tracked = [PY, "tools/check.py"]
    gone_test = [PY, "-m", "pytest", "tests/test_gone.py", "-q"]
    beside = ["bash", "-lc", "python probe_gone.py; python tools/check.py"]
    core, record = _gate_record(
        tmp_path, [(tracked, {"exit_code": 1}), (gone_test, {"exit_code": 1}), (beside, {"exit_code": 1})]
    )
    reasons = completion_gate.floor(core, record)
    assert any("tools/check.py" in reason for reason in reasons), reasons
    assert any("tests/test_gone.py" in reason for reason in reasons), reasons
    # A deleted probe beside a tracked script excuses nothing.
    assert any("probe_gone.py" in reason for reason in reasons), reasons
