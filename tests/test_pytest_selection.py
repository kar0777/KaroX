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
