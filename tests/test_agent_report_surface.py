"""The final report says why a run stopped and where the failure is.

A user should not have to open the session directory to learn that the approved
check failed and which test broke. These tests pin the contract at the report
and human-output layers; the kernel test that fills the fields is separate.
"""

from __future__ import annotations

from karox.agent_contract import AgentReport
from karox.cli import _print_agent_report
from karox.providers import ModelResponse
from karox.usage_analytics import merge_response_usage

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


def test_human_report_marks_an_unknown_cost(capsys) -> None:  # type: ignore[no-untyped-def]
    _print_agent_report(
        _report(
            usage={
                "total_tokens": 10,
                "cost_status": "unknown",
                "cost_unknown_reason": "usage_missing",
            }
        )
    )

    out = capsys.readouterr().out
    assert "costs: unknown (the provider did not report usage)" in out


def test_human_report_calls_a_partial_total_incomplete(capsys) -> None:  # type: ignore[no-untyped-def]
    _print_agent_report(
        _report(
            usage={
                "total_tokens": 10,
                "costs": {"USD": 0.01},
                "cost_status": "unknown",
                "cost_unknown_reason": "no_pricing",
            }
        )
    )

    out = capsys.readouterr().out
    assert "costs: USD=0.01" in out
    assert "costs: unknown (no pricing record for this model); the measured total is incomplete" in out


def _response(**overrides: object) -> ModelResponse:
    values: dict[str, object] = {
        "content": "answer",
        "tool_calls": (),
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    values.update(overrides)
    return ModelResponse(**values)  # type: ignore[arg-type]


def test_session_usage_keeps_unknown_cost_sticky() -> None:
    aggregate = merge_response_usage(
        {},
        _response(usage={}, cost=None, cost_unknown_reason="usage_missing"),
        event={"step": 1},
    )
    assert aggregate["cost_status"] == "unknown"
    assert aggregate["cost_unknown_reason"] == "usage_missing"

    aggregate = merge_response_usage(
        aggregate,
        _response(cost=0.01, currency="USD", cumulative_cost=0.01),
        event={"step": 2},
    )
    # One unpriced response means the session total is not a measured amount,
    # even after a priced one.
    assert aggregate["cost_status"] == "unknown"
    assert aggregate["costs"] == {"USD": 0.01}