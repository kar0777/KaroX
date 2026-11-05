"""Context economy of tool results and of the finish gate."""

from __future__ import annotations

from karox import completion_gate
from karox.engine_tool_env import DIFF_ECHO_MAX_CHARS, _compact_diff, _mutation_body


def _diff(lines: int) -> str:
    body = [
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -10,3 +10,%d @@" % (lines + 1),
        " context",
    ]
    body += [f"+added line {index} with some text" for index in range(lines)]
    body += ["-removed line", "@@ -80,2 +90,2 @@", "-old", "+new"]
    return "\n".join(body)


def test_a_short_diff_is_echoed_verbatim() -> None:
    small = _diff(2)
    assert len(small) <= DIFF_ECHO_MAX_CHARS
    assert _compact_diff(small) == small


def test_a_long_diff_is_summarized_with_its_hunk_locations() -> None:
    large = _diff(60)
    assert len(large) > DIFF_ECHO_MAX_CHARS
    summary = _compact_diff(large)
    assert summary == "diff: +61 -2 lines, 2 hunk(s) at new line 10, 90 (applied as sent)"


def test_mutation_body_carries_the_compact_diff() -> None:
    body = _mutation_body(
        "repo.edit_file", {"path": "app.py", "diff": _diff(60)}, True
    )
    assert "added line 59" not in body
    assert "2 hunk(s) at new line 10, 90" in body


def test_only_attestation_is_true_only_for_requirement_gaps() -> None:
    assert completion_gate.only_attestation(
        [
            "requirement R3 quotes a result received before the last edit",
            "missing requirement attestation for R7",
        ]
    )
    assert not completion_gate.only_attestation([])
    assert not completion_gate.only_attestation(
        ["requirement R3 is open", "missing visual evidence for index.html"]
    )
