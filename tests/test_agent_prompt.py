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
