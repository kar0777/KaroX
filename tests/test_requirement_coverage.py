"""Requirement coverage: a web deliverable is verified clause by clause.

The completion floor knew that checks passed and that a page rendered, never
whether each requested behaviour was exercised. A run could therefore report
VERIFIED while one sentence of a long bullet ("the score also grows over
time") was never implemented. These tests pin the deterministic parts: how
the ledger becomes atomic clauses, how an attestation is read from the finish
call, and which observations can back a ``verified`` entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from karox import completion_gate
from karox import requirement_coverage as coverage
from karox.requirements import seed


def _record(
    task: str,
    history: List[Dict[str, Any]],
    *,
    changed: tuple[str, ...] = ("app.js",),
) -> SimpleNamespace:
    return SimpleNamespace(
        task=task,
        requirements=seed(task),
        provider_history=history,
        changed_files=list(changed),
    )


def _edit(path: str) -> Dict[str, Any]:
    return {
        "role": "tool",
        "core_name": "repo.write_file",
        "content": "written",
        "result": {"ok": True, "data": {"changed": True, "path": path}},
    }


def _seen(content: str, core_name: str = "browser.command") -> Dict[str, Any]:
    return {
        "role": "tool",
        "core_name": core_name,
        "content": content,
        "result": {"ok": True, "data": {}},
    }


def _finish(requirements: Optional[Any] = None) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {"answer": "done"}
    if requirements is not None:
        arguments["requirements"] = requirements
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "call_id": "f1",
                "name": "finish",
                "raw_arguments": json.dumps(arguments, sort_keys=True),
            }
        ],
    }


TASK = "The score rises over time. A restart begins a fresh run. Keep the page offline."


def test_bullets_sentences_and_semicolons_become_clauses() -> None:
    text = (
        "- Three modes, one board; **P** pauses. Collecting a gem adds 50 points. "
        "The total also grows. - A HUD shows the total (top left; always) and the best total."
    )
    assert coverage.split_clauses(text) == [
        "Three modes, one board",
        "**P** pauses.",
        "Collecting a gem adds 50 points.",
        "The total also grows.",
        "A HUD shows the total (top left; always) and the best total.",
    ]


def test_headings_labels_and_fragments_are_not_clauses() -> None:
    assert coverage.split_clauses("## Layout") == []
    assert coverage.split_clauses("Stable ids:") == []
    assert coverage.split_clauses("TASK") == []
    assert coverage.split_clauses("Use e.g. a cube. Then stop.") == [
        "Use e.g. a cube.",
        "Then stop.",
    ]
    # A code span is a clause on its own; its punctuation never splits it.
    assert coverage.split_clauses('- `[data-id="a; b"]` - `run()` must be pure.') == [
        '`[data-id="a; b"]`',
        "`run()` must be pure.",
    ]


def test_clause_ids_are_stable_and_derived_from_the_ledger() -> None:
    entries = seed("Make it red.\n\nAdd a test. Keep the README.")
    assert coverage.clauses(entries) == [
        ("R1", "Make it red."),
        ("R2.1", "Add a test."),
        ("R2.2", "Keep the README."),
    ]
    entries[1]["status"] = "superseded"
    assert coverage.clauses(entries) == [("R1", "Make it red.")]


def test_a_huge_ledger_is_attested_per_requirement() -> None:
    task = "\n\n".join(
        " ".join(f"Rule {block} part {part} holds." for part in range(5)) for block in range(20)
    )
    listed = coverage.clauses(seed(task))
    assert len(listed) == 20
    assert listed[0][0] == "R1"


def test_no_finish_means_every_clause_is_unattested() -> None:
    record = _record(TASK, [_edit("app.js"), _seen("score 0 -> 12")])
    reasons = coverage.floor_reasons(record)
    assert reasons == ["missing requirement attestation: 3 requirements (R1.1, R1.2, R1.3)"]


def test_a_fresh_quote_discharges_its_clause() -> None:
    attestation = [
        {"id": "R1.1", "status": "verified", "observed": '"score": 12, "elapsed": 3'},
        {"id": "r1.2", "status": "verified", "observed": "PASS restart :: status=running score=0"},
        {"id": "R1.3", "status": "not_applicable", "observed": "no network is used anywhere"},
    ]
    record = _record(
        TASK,
        [
            _edit("app.js"),
            _seen('{"text": "PASS restart :: status=running score=0\\nPASS clock"}'),
            _seen('{"score":12,"elapsed":3}', core_name="checks.run"),
            _finish(attestation),
        ],
    )
    assert coverage.judge(record) == []
    assert coverage.floor_reasons(record) == []


def test_a_quote_must_come_from_a_behavioural_result_after_the_last_edit() -> None:
    attestation = [
        {"id": "R1.1", "status": "verified", "observed": "score rises by 12"},
        {"id": "R1.2", "status": "verified", "observed": "restart resets the run"},
        {"id": "R1.3", "status": "verified", "observed": "made up output text"},
    ]
    record = _record(
        TASK,
        [
            _seen("score rises by 12"),  # before the edit: stale
            _edit("app.js"),
            _seen("restart resets the run", core_name="repo.read_file"),  # source, not behaviour
            {"role": "tool", "core_name": None, "content": "made up output text"},  # a refusal
            _finish(attestation),
        ],
    )
    problems = {cid: problem for cid, _, problem in coverage.judge(record)}
    assert set(problems) == {"R1.1", "R1.2", "R1.3"}
    assert all("not in any" in problem for problem in problems.values())


def test_a_probe_page_edit_does_not_make_observations_stale() -> None:
    attestation = [
        {"id": cid, "status": "verified", "observed": "PASS all three facts"}
        for cid in ("R1.1", "R1.2", "R1.3")
    ]
    record = _record(
        TASK,
        [
            _edit("app.js"),
            _seen("PASS all three facts"),
            _edit("_probe.html"),
            _finish(attestation),
        ],
    )
    assert coverage.judge(record) == []


def test_an_edit_after_finish_reopens_every_clause() -> None:
    attestation = [
        {"id": cid, "status": "verified", "observed": "PASS all three facts"}
        for cid in ("R1.1", "R1.2", "R1.3")
    ]
    record = _record(
        TASK,
        [_edit("app.js"), _seen("PASS all three facts"), _finish(attestation), _edit("app.js")],
    )
    problems = coverage.judge(record)
    assert [problem for _, _, problem in problems] == ["attested before the last edit"] * 3


def test_weak_or_dishonest_entries_stay_open() -> None:
    attestation = {
        "R1.1": {"status": "verified", "observed": "true"},
        "R1.2": {"status": "unmet", "observed": "restart keeps the old score"},
        "R1.3": {"status": "not_applicable", "observed": "n/a"},
    }
    record = _record(TASK, [_edit("app.js"), _seen("true"), _finish(attestation)])
    problems = {cid: problem for cid, _, problem in coverage.judge(record)}
    assert problems == {
        "R1.1": "observed text too short to identify a result",
        "R1.2": "reported unmet",
        "R1.3": "not_applicable needs a reason",
    }
    reasons = coverage.floor_reasons(record)
    assert reasons[1] == "requirement R1.2 reported unmet: A restart begins a fresh run."


def test_the_call_being_made_is_judged_against_everything_seen_so_far() -> None:
    record = _record(TASK, [_edit("app.js"), _seen("PASS all three facts")])
    attestation = [
        {"id": cid, "status": "verified", "observed": "PASS all three facts"}
        for cid in ("R1.1", "R1.2", "R1.3")
    ]
    assert coverage.judge(record, attestation, use_transcript=False) == []
    problems = coverage.judge(record, attestation[:1], use_transcript=False)
    assert [cid for cid, _, _ in problems] == ["R1.2", "R1.3"]
    message = coverage.refusal_message(problems)
    assert "R1.2 [not attested] A restart begins a fresh run." in message
    assert '"requirements"' in message


def test_coverage_is_owed_only_by_a_web_deliverable_with_a_checklist(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    core = SimpleNamespace(repository=tmp_path)
    assert completion_gate.coverage_required(core, _record(TASK, [], changed=("app.js",)))
    # Code without a page, a probe page alone, and a short request owe nothing.
    assert not completion_gate.coverage_required(core, _record(TASK, [], changed=("app.py",)))
    assert not completion_gate.coverage_required(core, _record(TASK, [], changed=("_probe.html",)))
    assert not completion_gate.coverage_required(
        core, _record("Make the button red.", [], changed=("app.js",))
    )
    legacy = _record(TASK, [], changed=("app.js",))
    legacy.requirements = []
    assert not completion_gate.coverage_required(core, legacy)
