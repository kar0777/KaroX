"""The system prompt states verification rules, not one task's answers.

An earlier revision told every run to create obstacles and collectibles in
``reset()`` and to assert ``state().obstacles.length > 0`` and
``state().coins.length > 0`` -- the hidden check of a single evaluation task,
copied into the instructions of all tasks. What survives is the general rule:
a probe checks a concrete fact the task requires, a check that compares a value
to itself proves nothing, and the state after initialization or a reset must
match what the task itself specifies.
"""

from __future__ import annotations

from karox.agent import SYSTEM_PROMPT


def test_system_prompt_states_the_task_neutral_probe_rule() -> None:
    assert "concrete, observable facts the task requires" in SYSTEM_PROMPT
    assert "compares a value to itself" in SYSTEM_PROMPT
    assert "cannot fail, proves nothing" in SYSTEM_PROMPT
    assert "initial conditions the task specifies" in SYSTEM_PROMPT


def test_system_prompt_does_not_prescribe_one_task_state_shape() -> None:
    lowered = SYSTEM_PROMPT.lower()
    for fragment in (
        "state().obstacles",
        "state().coins",
        "obstacles and collectibles",
        "for a game, the ready",
        "make restart restore the same set",
    ):
        assert fragment not in lowered, fragment


def test_system_prompt_asks_for_short_reasoning_and_no_drafts() -> None:
    """Reasoning is output the run pays for and waits on.

    Replaying one recorded first-write request (the same messages, tools and
    reasoning setting) took about 90 s and 9-10K reasoning tokens, most of it a
    draft of the files the tool call then wrote again; with this rule the same
    request took 23-42 s and mostly 0.6-1.6K reasoning tokens for files of similar size.
    """
    # HARD12 (2026-11-11): "minimize reasoning" also cut the problem analysis;
    # KaroX trailed DeepSeek Harness on root cause and edge cases. Thinking about
    # the problem is allowed again; drafting files in reasoning is still not.
    assert "do not draft file contents" in SYSTEM_PROMPT
    # HARD12 gha3 (v19, "think as long as needed"): +60% tokens, same solve rate;
    # v20 keeps the economy rule and the root-cause/edge-case guidance.
    assert "correctness comes first" in SYSTEM_PROMPT
    assert "find the root cause" in SYSTEM_PROMPT
