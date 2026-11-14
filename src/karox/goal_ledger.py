"""After the maintainer review, a finish lists each task goal with the output that shows it.

v55 exstruct-113 / wtforms-892 / datamodel-3070: in every lost run the agent's
own probe printed that a goal did not hold yet ("xlwings loaded: True",
identical output for a pressed and an unpressed button) and the agent
finished anyway. Asking it to copy, per goal, the output line that proves it
makes that contradiction visible at the moment of finishing.

Bounded: one refusal per run, then the finish goes through; a run without
the review (low effort, no code change) is never asked.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from . import requirement_coverage as rc

FIELD = "goals"
REFUSALS = 1
_MIN_GOALS = 1


def parse(value: Any) -> List[Tuple[str, str]]:
    """(goal, observed) pairs from the finish argument; malformed entries are dropped."""

    pairs: List[Tuple[str, str]] = []
    if not isinstance(value, list):
        return pairs
    for entry in value:
        if isinstance(entry, Mapping):
            goal, observed = str(entry.get("goal") or "").strip(), str(entry.get("observed") or "").strip()
            if goal:
                pairs.append((goal, observed))
    return pairs


def problems(value: Any, history: Sequence[Mapping[str, Any]]) -> List[str]:
    """What keeps this finish from showing its goals; [] lets it through."""

    pairs = parse(value)
    if len(pairs) < _MIN_GOALS:
        return ["missing"]
    observations = rc.fresh_observations(history)
    runs = rc._copied_runs(observations)
    missing: List[str] = []
    for goal, observed in pairs:
        quote = rc.normalize(observed)
        if len(quote.split()) < 2 or not rc._quoted(quote, observations, runs):
            missing.append(goal)
    return missing


def refusal(missing: Sequence[str]) -> str:
    head = (
        'Before this finish: list each goal the task states in finish "goals": '
        '[{"goal": "<the task\'s goal in its words>", "observed": "<one line copied exactly from a '
        'checks_run or test output received after your last edit that shows the goal holds>"}].'
    )
    if list(missing) != ["missing"]:
        head = (
            "These goals quote no output received after your last edit: "
            + "; ".join(g[:120] for g in missing[:6])
            + ". " + head
        )
    return (
        head + " If no output shows a goal holding -- or your latest output shows it does not "
        "(a module still loaded, the same output for cases the feature must tell apart, an error "
        "path still reached) -- the work is not done: probe it, fix the code, then finish."
    )
