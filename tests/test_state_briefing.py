"""The durable state snapshot that survives compaction.

The block is a contract with a model that has just lost most of its history:
every line is a recorded fact, the section order is fixed, and the same session
state always produces the same text. The tests use the exact shapes the record
writes (requirements with ids, plan items with statuses, check entries with a
failure map), not invented ones.
"""

from __future__ import annotations

from karox.state_briefing import MAX_STATE_BRIEFING_CHARS, build_state_briefing


def _write(path: str = "calc.py") -> dict[str, object]:
    return {
        "role": "tool",
        "core_name": "repo.write_file",
        "result": {"ok": True, "data": {"changed": True, "path": path}},
    }


def _check(
    *,
    failed: bool = True,
    timed_out: bool = False,
    argv: tuple[str, ...] = ("python", "-m", "pytest", "-q"),
    failures: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "verification_eligible": True,
        "exit_code": 1 if failed else 0,
        "timed_out": timed_out,
        "argv": list(argv),
    }
    if failures is not None:
        data["failure_map"] = {"failures": failures, "total": len(failures)}
    return {
        "role": "tool",
        "core_name": "checks.run",
        "result": {"ok": not failed, "data": data},
    }


CALC_FAILURES = [
    {
        "path": "tests/test_calc.py",
        "line": 5,
        "test_id": "tests/test_calc.py::test_add",
        "message": "assert 0 == 4",
    }
]


def test_full_snapshot_lists_requirements_plan_and_failed_test() -> None:
    text = build_state_briefing(
        task="1. Fix add()\n2. Keep the tests green",
        requirements=[
            {"id": "R1", "text": "Fix add()", "status": "active"},
            {"id": "R2", "text": "Keep the tests green", "status": "active"},
            {"id": "R3", "text": "Old wording", "status": "superseded"},
        ],
        plan_items=[
            {"text": "implement add", "status": "doing"},
            {"text": "run tests", "status": "done"},
        ],
        changed_files=["calc.py"],
        history=[_write(), _check(failures=CALC_FAILURES)],
    )

    assert text.startswith("[KaroX durable state")
    assert "Task: 1. Fix add() 2. Keep the tests green" in text
    assert "- R1 Fix add()" in text
    assert "- R2 Keep the tests green" in text
    assert "R3" not in text
    assert "- [doing] implement add" in text
    assert "run tests" not in text
    assert "Changed files (1): calc.py" in text
    assert "Verification: `python -m pytest -q` failed" in text
    assert "tests/test_calc.py:5 · test_add — assert 0 == 4" in text
    assert "Change review: no git.diff review since the last edit." in text


def test_single_requirement_equal_to_the_task_is_not_repeated() -> None:
    text = build_state_briefing(
        task="create hello.txt with hi",
        requirements=[
            {"id": "R1", "text": "create hello.txt with hi", "status": "active"}
        ],
    )

    assert "Requirements still active" not in text
    assert text.count("create hello.txt with hi") == 1


def test_no_check_yet_is_stated_plainly() -> None:
    text = build_state_briefing(
        task="fix the parser",
        changed_files=["parser.py"],
    )

    assert "Verification: no approved check has run yet" in text


def test_passed_check_is_reported_as_passed() -> None:
    text = build_state_briefing(
        task="fix the parser",
        history=[_check(failed=False)],
    )

    assert "Verification: `python -m pytest -q` passed" in text
    assert "Failures:" not in text


def test_timed_out_check_is_named() -> None:
    text = build_state_briefing(
        task="fix the parser",
        history=[_check(timed_out=True)],
    )

    assert "timed out" in text


def test_failed_check_without_a_map_says_so() -> None:
    text = build_state_briefing(
        task="fix the parser",
        history=[_check(failures=None)],
    )

    assert "Failures: none parsed" in text


def test_review_after_the_edit_is_reported() -> None:
    history = [
        _write(),
        {"role": "tool", "core_name": "git.diff", "result": {"ok": True, "data": {}}},
    ]
    text = build_state_briefing(task="fix it", history=history)

    assert "Change review: git.diff ran after the last edit." in text


def test_low_value_sections_drop_before_the_cap() -> None:
    text = build_state_briefing(
        task="t" * 900,
        requirements=[
            {"id": f"R{index}", "text": "r" * 200, "status": "active"}
            for index in range(1, 30)
        ],
        plan_items=[
            {"text": "p" * 200, "status": "todo"} for _ in range(20)
        ],
        changed_files=[f"file_{index}.py" for index in range(40)],
        history=[_check(failures=CALC_FAILURES)],
    )

    assert len(text) <= MAX_STATE_BRIEFING_CHARS
    # The verification line and its failure survive every drop.
    assert "Verification:" in text
    assert "tests/test_calc.py:5" in text


def test_same_state_renders_the_same_text() -> None:
    arguments = dict(
        task="1. Fix add()\n2. Keep the tests green",
        requirements=[
            {"id": "R1", "text": "Fix add()", "status": "active"},
            {"id": "R2", "text": "Keep the tests green", "status": "active"},
        ],
        plan_items=[{"text": "implement add", "status": "doing"}],
        changed_files=["calc.py"],
        history=[_write(), _check(failures=CALC_FAILURES)],
    )

    assert build_state_briefing(**arguments) == build_state_briefing(**arguments)


def test_empty_state_has_no_block() -> None:
    assert build_state_briefing(task="") == ""