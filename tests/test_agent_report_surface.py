"""The final report says why a run stopped and where the failure is.

A user should not have to open the session directory to learn that the approved
check failed and which test broke. These tests pin the contract at the report
and human-output layers; the kernel test that fills the fields is separate.
"""

from __future__ import annotations

from karox.agent_contract import AgentReport
from karox.cli import _print_agent_report

FAILURE_MAP = {
    "failures": [
        {
            "runner": "pytest",
            "path": "tests/test_a.py",
            "line": 5,
            "test_id": "tests/test_a.py::test_a",
            "message": "assert 1 == 2",
        }
    ],
    "total": 1,
    "repeats": 0,
    "runners": ["pytest"],
}


def _report(**overrides: object) -> AgentReport:
    values: dict[str, object] = {
        "session_id": "s1",
        "status": "stopped",
        "phase": "verification",
        "verified": False,
        "reason": "verification_failed",
        "steps": 3,
        "changed_files": ("a.py",),
        "checks": (),
        "git_state": {},
        "evidence": (),
        "usage": {},
        "provider_message": None,
        "unmet": ("active failed check: python -m pytest -q",),
        "failure_map": FAILURE_MAP,
    }
    values.update(overrides)
    return AgentReport(**values)  # type: ignore[arg-type]


def test_report_serializes_unmet_and_failure_map() -> None:
    payload = _report().to_dict()

    assert payload["unmet"] == ["active failed check: python -m pytest -q"]
    assert payload["failure_map"]["failures"][0]["line"] == 5
    assert payload["failure_map"]["total"] == 1


def test_human_report_prints_the_reason_and_the_failing_place(capsys) -> None:  # type: ignore[no-untyped-def]
    _print_agent_report(_report())

    out = capsys.readouterr().out
    assert "unmet: active failed check: python -m pytest -q" in out
    assert "KaroX failure map" in out
    assert "tests/test_a.py:5" in out


def test_verified_report_has_nothing_unmet() -> None:
    payload = _report(
        status="verified",
        verified=True,
        reason="submitted",
        unmet=(),
        failure_map=None,
    ).to_dict()

    assert payload["unmet"] == []
    assert payload["failure_map"] is None