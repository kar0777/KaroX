"""First-class Effort: one user knob that really changes the runtime.

The product contract (v5-final-product-pass, sections 20-23) is explicit:
``/effort`` is *not* a prompt string. Every level maps to concrete, validated
runtime budgets -- agent step/time ceilings, context economy, investigation
breadth, hypothesis count, verification depth, Git-history depth, subagent and
research allowances -- so ``effort=ultra`` buys measurably more engineering,
never just "please try harder".

Design notes.

* Levels are ordered and every numeric budget is monotonically non-decreasing
  across that order. ``tests/test_effort.py`` enforces this so a future edit
  cannot quietly make HIGH cheaper than MEDIUM in one dimension.
* ``AgentLimits`` and ``ContextBudget`` already validate their own ranges at
  construction time. The tables below are built through those constructors on
  import, so an out-of-range budget is an import error in CI, not a runtime
  surprise in a user session.
* The provider knob (``reasoning_effort``) is one *output* of a level, mapped
  onto the provider vocabulary from :data:`karox.providers.REASONING_EFFORTS`.
  Effort must keep working for providers that ignore reasoning hints, which is
  why the rest of the budget never depends on the provider honouring it.
* AUTO is a recommendation, not a silent decision: it returns the level plus
  human-readable reasons, and the caller (TUI or CLI) shows them and lets the
  user override. That is the section-23 contract.

Maturity: FOUNDATION (this module, tested) -> WIRED happens where the TUI and
agent construction consume :func:`budget_for`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .agent import AgentLimits, ContextBudget
from .providers import REASONING_EFFORTS

# The user-facing ladder, in ascending order of spend. "auto" is deliberately
# not a member: it resolves to one of these and never reaches a budget table.
EFFORT_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "extra-high", "ultra")

AUTO_EFFORT = "auto"

DEFAULT_EFFORT = "medium"

# Spellings users actually type, plus the provider vocabulary, so a person who
# says "xhigh" (the wire spelling) or "extra_high" (shell-friendly) is not
# lectured about hyphens. Aliases resolve to canonical levels only.
_ALIASES: Dict[str, str] = {
    "auto": AUTO_EFFORT,
    "low": "low",
    "medium": "medium",
    "normal": "medium",
    "default": "medium",
    "high": "high",
    "extra-high": "extra-high",
    "extra_high": "extra-high",
    "extrahigh": "extra-high",
    "x-high": "extra-high",
    "xhigh": "extra-high",
    "ultra": "ultra",
    "max": "ultra",
}

# Level -> provider reasoning vocabulary. Validated against REASONING_EFFORTS
# at import time below: if the provider ladder ever changes shape this module
# refuses to import rather than sending an unknown value on the wire.
_REASONING_BY_LEVEL: Dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra-high": "xhigh",
    "ultra": "max",
}

for _level, _value in _REASONING_BY_LEVEL.items():
    if _value not in REASONING_EFFORTS:
        raise RuntimeError(
            f"effort level {_level!r} maps to unknown reasoning effort {_value!r}"
        )


def normalize_effort(value: object) -> str:
    """Return the canonical effort level (or ``auto``) for user input.

    Raises ``ValueError`` with the full menu for anything unrecognisable, so a
    caller can show the message verbatim instead of inventing its own.
    """

    if not isinstance(value, str):
        raise ValueError(_unknown_effort_message(repr(value)))
    cleaned = value.strip().casefold().replace(" ", "")
    resolved = _ALIASES.get(cleaned)
    if resolved is None:
        raise ValueError(_unknown_effort_message(value.strip() or repr(value)))
    return resolved


def _unknown_effort_message(shown: str) -> str:
    menu = ", ".join((AUTO_EFFORT, *EFFORT_LEVELS))
    return f"unknown effort {shown!r}: expected one of {menu}"


@dataclass(frozen=True)
class EffortBudget:
    """Every runtime consequence of one effort level, in one place.

    ``agent_limits`` and ``context`` are real constructor-validated objects,
    not loose numbers, so wiring them into ``Agent(...)`` cannot drift from
    their own invariants. The remaining fields parameterise the behaviors the
    master mandate lists in section 22; consumers read them from here instead
    of hard-coding per-level ``if`` chains.
    """

    level: str
    reasoning_effort: str
    agent_limits: AgentLimits
    context: ContextBudget
    # How many distinct files/regions the agent should be willing to inspect
    # before it must either act or explain why more reading is needed.
    investigation_breadth: int
    # Competing root-cause hypotheses to hold before committing to a fix; the
    # section-18 discriminating-test discipline needs at least two at HIGH+.
    hypothesis_budget: int
    # Verification ladder rung from section 19 (1 static .. 5 live acceptance).
    verification_rung: int
    # Project-map pass depth this level is entitled to request.
    map_depth: str
    # Commits of Git history the agent may mine for evidence (0 = none).
    git_history_depth: int
    # Parallel subagents this level may spawn (0 = stay single-threaded).
    allowed_subagents: int
    # External research/documentation lookups budgeted for the task.
    research_calls: int
    # Test scope the level must run before claiming success.
    test_breadth: str

    def __post_init__(self) -> None:
        if self.level not in EFFORT_LEVELS:
            raise ValueError(f"budget level must be one of {EFFORT_LEVELS}")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning effort must be one of " + ", ".join(REASONING_EFFORTS)
            )
        if self.test_breadth not in ("targeted", "affected", "full"):
            raise ValueError("test breadth must be targeted, affected, or full")
        if self.map_depth not in ("low", "medium", "high", "extra-high", "ultra"):
            raise ValueError("map depth must be a valid map level")
        if self.verification_rung not in (1, 2, 3, 4, 5):
            raise ValueError("verification rung must be 1..5")
        for name in (
            "investigation_breadth",
            "hypothesis_budget",
            "git_history_depth",
            "allowed_subagents",
            "research_calls",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def apply_to_context(self, base: ContextBudget) -> ContextBudget:
        """Overlay this level's context economy onto a model-aware base.

        The model's real input window (``max_input_tokens``) belongs to the
        registry, not to effort, so it is preserved from ``base``; effort only
        decides how much of that window a turn may use and how much tool
        output is kept verbatim.
        """

        return dataclasses.replace(
            base,
            utilization=self.context.utilization,
            keep_recent_groups=self.context.keep_recent_groups,
            max_tool_result_chars=self.context.max_tool_result_chars,
        )


# The budget tables. Built through validating constructors on import (see
# module docstring). Numbers grow monotonically with the ladder; the tests
# assert it field by field.
_BUDGETS: Dict[str, EffortBudget] = {
    "low": EffortBudget(
        level="low",
        reasoning_effort="low",
        agent_limits=AgentLimits(max_steps=12, max_seconds=300.0),
        context=ContextBudget(
            utilization=0.5, keep_recent_groups=6, max_tool_result_chars=16_000
        ),
        investigation_breadth=6,
        hypothesis_budget=1,
        verification_rung=2,
        map_depth="low",
        git_history_depth=0,
        allowed_subagents=0,
        research_calls=0,
        test_breadth="targeted",
    ),
    "medium": EffortBudget(
        level="medium",
        reasoning_effort="medium",
        agent_limits=AgentLimits(max_steps=24, max_seconds=900.0),
        context=ContextBudget(
            utilization=0.6, keep_recent_groups=8, max_tool_result_chars=24_000
        ),
        investigation_breadth=16,
        hypothesis_budget=2,
        verification_rung=3,
        map_depth="medium",
        git_history_depth=10,
        allowed_subagents=0,
        research_calls=2,
        test_breadth="affected",
    ),
    "high": EffortBudget(
        level="high",
        reasoning_effort="high",
        agent_limits=AgentLimits(max_steps=48, max_seconds=1800.0),
        context=ContextBudget(
            utilization=0.7, keep_recent_groups=10, max_tool_result_chars=32_000
        ),
        investigation_breadth=40,
        hypothesis_budget=3,
        verification_rung=4,
        map_depth="high",
        git_history_depth=50,
        allowed_subagents=2,
        research_calls=5,
        test_breadth="affected",
    ),
    "extra-high": EffortBudget(
        level="extra-high",
        reasoning_effort="xhigh",
        agent_limits=AgentLimits(max_steps=72, max_seconds=2700.0),
        context=ContextBudget(
            utilization=0.75, keep_recent_groups=12, max_tool_result_chars=48_000
        ),
        investigation_breadth=80,
        hypothesis_budget=4,
        verification_rung=4,
        map_depth="extra-high",
        git_history_depth=200,
        allowed_subagents=4,
        research_calls=10,
        test_breadth="full",
    ),
    "ultra": EffortBudget(
        level="ultra",
        reasoning_effort="max",
        agent_limits=AgentLimits(max_steps=96, max_seconds=3600.0),
        context=ContextBudget(
            utilization=0.8, keep_recent_groups=16, max_tool_result_chars=64_000
        ),
        investigation_breadth=160,
        hypothesis_budget=6,
        verification_rung=5,
        map_depth="ultra",
        git_history_depth=1_000,
        allowed_subagents=8,
        research_calls=20,
        test_breadth="full",
    ),
}


@dataclass(frozen=True)
class TaskSignals:
    """What AUTO effort is allowed to look at, per mandate section 23.

    Everything defaults to "unknown/small" so a caller that knows nothing gets
    the default level rather than an accidental escalation.
    """

    ambiguity: bool = False
    files_likely_affected: int = 0
    dependency_breadth: int = 0
    risk_area: bool = False
    migration_involved: bool = False
    test_complexity: bool = False
    regression_history: bool = False
    # Supplied by the deriver (karox.effort_signals), not typed by a person:
    # the current mode and whether the stored project map is stale for the
    # repository revision. Defaults mean "unknown" and keep old callers
    # bit-for-bit stable.
    mode: Optional[str] = None
    map_stale: bool = False


@dataclass(frozen=True)
class EffortRecommendation:
    level: str
    reasons: Tuple[str, ...]

    @property
    def budget(self) -> EffortBudget:
        return budget_for(self.level)


def recommend_effort(signals: Optional[TaskSignals] = None) -> EffortRecommendation:
    """Resolve AUTO into a level plus the reasons a person can audit.

    Deterministic scoring, no model call: AUTO has to be explainable and
    reproducible in tests. Each triggered signal adds weight and one reason
    line; thresholds map the total onto the ladder. The user always sees the
    reasons and may override -- this function recommends, it does not decide.
    """

    if signals is None:
        return EffortRecommendation(
            level=DEFAULT_EFFORT,
            reasons=("no task signals provided; defaulting to medium",),
        )

    score = 0
    reasons = []
    if signals.ambiguity:
        score += 2
        reasons.append("task statement is ambiguous")
    if signals.files_likely_affected > 20:
        score += 3
        reasons.append(f"~{signals.files_likely_affected} files likely affected")
    elif signals.files_likely_affected > 5:
        score += 2
        reasons.append(f"~{signals.files_likely_affected} files likely affected")
    elif signals.files_likely_affected > 1:
        score += 1
        reasons.append(f"~{signals.files_likely_affected} files likely affected")
    if signals.dependency_breadth > 10:
        score += 3
        reasons.append(f"{signals.dependency_breadth} dependent modules")
    elif signals.dependency_breadth > 3:
        score += 2
        reasons.append(f"{signals.dependency_breadth} dependent modules")
    if signals.risk_area:
        score += 3
        reasons.append("touches a high-risk subsystem (auth/security/data)")
    if signals.migration_involved:
        score += 2
        reasons.append("migration involved")
    if signals.test_complexity:
        score += 1
        reasons.append("complex test surface")
    if signals.regression_history:
        score += 2
        reasons.append("area has a history of regressions")
    if signals.map_stale:
        score += 1
        reasons.append("project map is stale; extra verification headroom")
    if signals.mode == "plan" and signals.ambiguity:
        score += 1
        reasons.append("plan mode on an ambiguous scope: broader investigation")

    if score >= 9:
        level = "ultra"
    elif score >= 7:
        level = "extra-high"
    elif score >= 4:
        level = "high"
    elif score >= 1:
        level = "medium"
    else:
        level = "low"
        reasons.append("no escalation signals; targeted change")
    if (
        signals.mode == "ideate"
        and EFFORT_LEVELS.index(level) > EFFORT_LEVELS.index("high")
    ):
        # Ideation never mutates and never runs the live verification ladder,
        # so recommending ultra would spend budget the mode cannot use. The
        # user's explicit choice still always wins over this cap.
        level = "high"
        reasons.append("ideate mode never mutates; capped at high")
    return EffortRecommendation(level=level, reasons=tuple(reasons))


def budget_for(
    level: str, signals: Optional[TaskSignals] = None
) -> EffortBudget:
    """Return the budget for a level, resolving ``auto`` via the signals."""

    resolved = normalize_effort(level)
    if resolved == AUTO_EFFORT:
        resolved = recommend_effort(signals).level
    return _BUDGETS[resolved]


def effort_summary(level: str, language: str = "en") -> str:
    """One status line for ``/status`` and the model picker: honest numbers."""

    budget = budget_for(level)
    if language == "ru":
        return (
            f"Effort {budget.level}: до {budget.agent_limits.max_steps} шагов, "
            f"до {int(budget.agent_limits.max_seconds)} с, "
            f"проверка уровня {budget.verification_rung}/5, "
            f"тесты: {budget.test_breadth}"
        )
    return (
        f"Effort {budget.level}: up to {budget.agent_limits.max_steps} steps, "
        f"up to {int(budget.agent_limits.max_seconds)}s, "
        f"verification rung {budget.verification_rung}/5, "
        f"tests: {budget.test_breadth}"
    )
