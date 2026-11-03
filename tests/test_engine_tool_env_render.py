from __future__ import annotations

from types import SimpleNamespace

from karox.engine_tool_env import PYTEST_MISSING_HINT, CoreToolEnvironment
from karox.models import CoreResult


def _render(core_name: str, data: dict) -> str:
    env = SimpleNamespace(config=SimpleNamespace(output_char_limit=20_000))
    result = CoreResult(ok=False, command=core_name, data=data, correlation_id="c-1")
    return CoreToolEnvironment._render_output(env, core_name, result)["output"]


def test_a_missing_pytest_is_named_as_a_runner_problem() -> None:
    """The django venv has no pytest; the model read it as a red test."""

    output = _render(
        "tests.run",
        {
            "exit_code": 1,
            "stdout": "",
            "stderr": "/repo/.venv/bin/python: No module named pytest\n",
            "suite": "focused",
            "target_count": 1,
        },
    )

    assert output.startswith(PYTEST_MISSING_HINT)
    assert "checks.run" in output


def test_an_ordinary_red_test_run_gets_no_runner_hint() -> None:
    output = _render(
        "tests.run",
        {
            "exit_code": 1,
            "stdout": "FAILED tests/test_a.py::test_x - assert 1 == 2\n",
            "stderr": "",
            "suite": "focused",
            "target_count": 1,
        },
    )

    assert PYTEST_MISSING_HINT not in output
    assert output.startswith("[tests.run exit 1")
