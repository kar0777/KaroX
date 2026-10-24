"""Deferred tool universe: which tool families one inference gets to see.

Advertising every tool schema on every request spends input tokens on
capabilities most turns never touch. The safe direction of error is known:
omitting a schema is recoverable (Core resolution still executes any
permitted tool called by exact name, and the family is attached from the
next step), while silently hiding a capability with no path back is not.
This module therefore decides *advertisement*, never *permission*.

The decision is a pure function of the task text. Same text, same result,
byte for byte: prompt-cache reuse depends on the selected universe staying
stable for the whole run unless the model itself proves it needs more.

This is the per-inference advertisement policy for the native agent kernel.
It deliberately complements ``tool_catalog`` (the profile-scoped diagnostics
grouping for the hosted bridge): names are normalized so both spellings of a
family resolve identically, but ``command.run`` style process execution is
grouped as ``process`` here because for one inference it is an infra lane,
not a coding-core lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

# Advertisement families in their one fixed presentation order. Order is part
# of the contract: equal selections must always render byte-identical
# schema payloads, or the prompt-cache prefix would churn for no reason.
FAMILY_ORDER: tuple[str, ...] = (
    "core",
    "task",
    "memory",
    "disk",
    "browser",
    "process",
    "admin",
)

# Families every inference receives. The coding lane (repo/git/tests/checks)
# is the product's center of gravity, and task/memory tools are how a run
# stays recoverable; deferring those would save bytes by making the agent
# worse, which is the one trade this subsystem is forbidden to make.
ALWAYS_FAMILIES: tuple[str, ...] = ("core", "task", "memory")

# First matching prefix wins; tuples so the resolution order is reviewable.
# Kernel-space names are dotted without the ``karox.`` prefix; hosted names
# carry it. ``family_of`` strips the prefix so both resolve identically.
_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("disk.", "disk"),
    ("browser.", "browser"),
    ("dev_server.", "process"),
    ("dev.", "process"),
    ("process.", "process"),
    ("command.", "process"),
    ("memory.", "memory"),
    ("task.", "task"),
    ("runtime.", "admin"),
    ("bridge.", "admin"),
    ("report.", "admin"),
)

# Conditional families and the task-text evidence that turns each one on.
# Word-stem alternations, English and Russian, matched case-insensitively.
# A false positive only costs the bytes of one family's schemas; a false
# negative is recovered by discovery, so the stems stay deliberately narrow.
_FAMILY_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "disk",
        re.compile(
            r"(?:\b(?:disk|drive|storage|cleanup|clean\s?up|free\s+space|delete|remove|cache|temp)\b"
            r"|\b(?:диск|накопител|очист|освобод|удал|мусор|кэш|кеш|временн)\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "browser",
        re.compile(
            r"(?:\b(?:browser|web\s?site|webpage|web\s?page|url|screenshot|"
            r"click|tab|dom|login|navigate|download|upload|html)\b"
            r"|https?://"
            r"|\b(?:браузер|сайт|страниц|вкладк|скриншот|клик|ссылк|"
            r"логин|скача|загруз|веб)\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "process",
        re.compile(
            r"(?:\b(?:server|process|daemon|service|port|restart|launch)\b"
            r"|\b(?:сервер|процесс|порт|перезапус|запуст|служб|демон)\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "admin",
        re.compile(
            r"(?:\b(?:runtime|diagnostics?|bridge|report)\b"
            r"|\b(?:рантайм|диагностик|бридж|отч[её]т)\w*)",
            re.IGNORECASE,
        ),
    ),
)

# One bounded system-prompt line must stay one line; a discovery note that
# grows past this ceiling lists a truncation marker instead of more names.
_NOTE_CEILING_CHARS = 1200


@dataclass(frozen=True)
class UniverseSelection:
    """The deterministic advertisement decision for one task.

    ``families`` and ``omitted`` partition FAMILY_ORDER; ``reasons`` records
    which stem turned each conditional family on, so a selection is always
    explainable after the fact instead of being a black box.
    """

    families: tuple[str, ...]
    omitted: tuple[str, ...]
    reasons: tuple[str, ...]


def family_of(tool_name: str) -> str:
    """Return the single advertisement family a tool belongs to.

    Unknown prefixes land in ``core`` rather than raising: a new tool family
    must degrade to "always advertised", never to "never advertised".
    """

    name = tool_name[6:] if tool_name.startswith("karox.") else tool_name
    for prefix, family in _FAMILY_PREFIXES:
        if name.startswith(prefix):
            return family
    return "core"


def select_families(task_text: str) -> UniverseSelection:
    """Decide which families this task's inferences should see.

    Pure and deterministic: the same text always yields the same selection,
    with families in FAMILY_ORDER. Signals only ever add families on top of
    ALWAYS_FAMILIES; nothing in the task text can remove the coding lane.
    """

    text = task_text if isinstance(task_text, str) else ""
    active = set(ALWAYS_FAMILIES)
    reasons: list[str] = []
    for family, pattern in _FAMILY_SIGNALS:
        match = pattern.search(text)
        if match is not None:
            active.add(family)
            reasons.append(f"{family}: task mentions {match.group(0)!r}")
    families = tuple(name for name in FAMILY_ORDER if name in active)
    omitted = tuple(name for name in FAMILY_ORDER if name not in active)
    return UniverseSelection(
        families=families, omitted=omitted, reasons=tuple(reasons)
    )


def discovery_note(omitted_tools: Mapping[str, Sequence[str]]) -> str:
    """Render the safe-discovery line for schemas that were not attached.

    The note names every omitted tool exactly once, in family order with
    sorted names, so the model can call one by exact name instead of hitting
    a dead end. Names only, never schemas: the whole point is that the bytes
    of the schema bodies stay off the request until a family is demanded.
    """

    parts: list[str] = []
    for family in FAMILY_ORDER:
        names = omitted_tools.get(family)
        if not names:
            continue
        # Brackets keep the family/name boundary unambiguous while costing
        # fewer bytes than prose and repeated punctuation on every turn.
        parts.append(f"{family}[{','.join(sorted(names))}]")
    if not parts:
        return ""
    note = (
        "Deferred tool schemas: " + ";".join(parts) + ". Call by exact "
        "name with JSON; that family's schemas attach next step."
    )
    if len(note) > _NOTE_CEILING_CHARS:
        note = note[: _NOTE_CEILING_CHARS - 22] + " ... (list truncated)."
    return note
