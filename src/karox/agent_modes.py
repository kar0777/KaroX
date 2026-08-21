"""First-class agent modes: Build, Plan, and Ideate.

A mode is the agent's default *stance* toward the project, orthogonal to the
effort ladder (``effort.py``): Build + Low and Ideate + Ultra are both valid.
The mode owns three things, and only these three, so it stays testable:

* a compact system-prompt delta (never a fork of the whole Constitution);
* the default mutation policy (may the agent touch production code without a
  fresh user gate?);
* the durable artifact the mode is expected to produce, if any.

The TUI/CLI persist the selected mode and thread it into agent construction
the same way the effort level travels. Transitions are free: any mode may
follow any other, the runtime may *recommend* a transition but never switches
silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MODES: tuple[str, ...] = ("build", "plan", "ideate")

DEFAULT_MODE = "build"

_ALIASES = {
    "build": "build",
    "b": "build",
    "plan": "plan",
    "p": "plan",
    "ideate": "ideate",
    "i": "ideate",
    "idea": "ideate",
}


class ModeError(ValueError):
    """An unknown agent mode was requested."""


def normalize_mode(value: object) -> str:
    """Return the canonical mode id for ``value`` or raise :class:`ModeError`."""

    if not isinstance(value, str) or not value.strip():
        raise ModeError(f"unknown agent mode: {value!r}")
    key = value.strip().lower().lstrip("/")
    mode = _ALIASES.get(key)
    if mode is None:
        raise ModeError(f"unknown agent mode: {value!r}")
    return mode


@dataclass(frozen=True)
class ModePolicy:
    """What a mode changes about real agent behavior.

    ``mutates_by_default`` is the enforcement-relevant bit: Plan and Ideate
    must not touch production code without an explicit user gate; Build may.
    ``default_artifact`` names the durable artifact the mode should leave
    behind (``plan`` / ``concept``), owned by the workstream so recovering it
    never requires transcript replay.
    """

    mode: str
    mutates_by_default: bool
    default_artifact: Optional[str]
    prompt_delta: str


_BUILD_DELTA = """MODE: BUILD.
You are executing. Understand the task, inspect the relevant project state,
implement, test, and verify. Continue until acceptance criteria hold or you
hit a genuine blocker worth reporting. Do not drift into open-ended planning:
plan exactly as much as the next concrete step needs. Code written is not
feature complete; module existing is not runtime wired; a passing unit test
is not a working product. Verify at the product path."""

_PLAN_DELTA = """MODE: PLAN.
Do not mutate production code by default; a user gate moves you to Build.
Inspect the project, clarify genuine ambiguity, identify the architecture,
compare viable approaches, and produce a durable Plan artifact: files and
modules to change, tests and migrations, verification steps, risks, and
rollback. The plan must be concrete enough that Build can start without
re-deriving it. Recommend Build when the plan is ready; never switch modes
yourself."""

_IDEATE_DELTA = """MODE: IDEATE.
Your primary objective is not implementation. Discover valuable
opportunities. Understand the current product behavior, the user's goals,
existing friction, architectural constraints, and adjacent opportunities.
Generate an idea only when there is a defensible reason for it to exist.
Challenge your own proposals and reject weak or generic ones: prefer three
strong ideas over thirty feature suggestions. Record surviving ideas as a
durable Concept artifact with problem, evidence, trade-offs, recommended
direction, risks, and open questions. Do not implement until the user picks
an idea or explicitly asks for Build."""


_POLICIES = {
    "build": ModePolicy(
        mode="build",
        mutates_by_default=True,
        default_artifact=None,
        prompt_delta=_BUILD_DELTA,
    ),
    "plan": ModePolicy(
        mode="plan",
        mutates_by_default=False,
        default_artifact="plan",
        prompt_delta=_PLAN_DELTA,
    ),
    "ideate": ModePolicy(
        mode="ideate",
        mutates_by_default=False,
        default_artifact="concept",
        prompt_delta=_IDEATE_DELTA,
    ),
}


def mode_policy(mode: object) -> ModePolicy:
    """Return the :class:`ModePolicy` for ``mode`` (normalized first)."""

    return _POLICIES[normalize_mode(mode)]


def mode_prompt_delta(mode: object) -> str:
    """Return the compact system-prompt delta for ``mode``."""

    return mode_policy(mode).prompt_delta


_MODE_WORDS = {
    "build": {"ru": "Сборка", "en": "Build"},
    "plan": {"ru": "План", "en": "Plan"},
    "ideate": {"ru": "Идеи", "en": "Ideate"},
}


def mode_display_name(mode: object, language: str = "en") -> str:
    """Human name for the status row and the TUI header."""

    words = _MODE_WORDS[normalize_mode(mode)]
    return words.get(language, words["en"])


_MODE_SUMMARIES = {
    "build": {
        "en": (
            "Build: implement, test, and verify; mutations follow the "
            "normal safety policy."
        ),
        "ru": (
            "Сборка: реализация, тесты и проверка; изменения идут по "
            "обычной политике безопасности."
        ),
    },
    "plan": {
        "en": (
            "Plan: no production-code mutation by default; leaves a durable "
            "Plan artifact. Build starts only on your explicit go-ahead."
        ),
        "ru": (
            "План: по умолчанию без изменений кода; создаёт долговременный "
            "артефакт плана. Сборка — только после вашего явного решения."
        ),
    },
    "ideate": {
        "en": (
            "Ideate: no production-code mutation by default; hunts for "
            "opportunities and records durable Concept artifacts."
        ),
        "ru": (
            "Идеи: по умолчанию без изменений кода; ищет возможности и "
            "сохраняет долговременные артефакты концепций."
        ),
    },
}


def mode_summary(mode: object, language: str = "en") -> str:
    """One honest sentence about what the mode changes, for /mode and /status."""

    texts = _MODE_SUMMARIES[normalize_mode(mode)]
    return texts.get(language, texts["en"])
