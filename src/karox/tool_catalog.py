"""Deterministic tool-catalog groups per profile (P1.5, frozen plan).

The catalog is profile-scoped and immutable for the lifetime of a bridge
process. What was missing is an explicit, deterministic statement of *which
family* each advertised tool belongs to, so operators and clients can predict
availability without reverse-engineering prefixes ("no hidden magic").

Grouping is a pure function of the internal dotted tool name. Every tool lands
in exactly one group; an unknown prefix lands in ``core`` rather than raising,
because a new tool family must never break diagnostics for older clients.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# Ordered by specificity: the first matching prefix wins. Tuples, not a dict,
# so the resolution order is part of the contract and visible in review.
_GROUP_PREFIXES: tuple[tuple[str, str], ...] = (
    ("karox.browser.", "browser"),
    ("karox.dev_server.", "devserver"),
    ("karox.memory.", "memory"),
    ("karox.task.", "task"),
    ("karox.runtime.", "admin"),
    ("karox.bridge.", "admin"),
    ("karox.repo.", "core"),
    ("karox.git.", "core"),
    ("karox.tests.", "core"),
    ("karox.checks.", "core"),
    ("karox.artifact.", "core"),
    ("karox.command.", "core"),
)

GROUP_NAMES: tuple[str, ...] = (
    "core",
    "task",
    "memory",
    "browser",
    "devserver",
    "admin",
)


def tool_group(internal_name: str) -> str:
    """Return the single deterministic group an internal tool name belongs to."""
    for prefix, group in _GROUP_PREFIXES:
        if internal_name.startswith(prefix):
            return group
    return "core"


def catalog_groups(internal_names: Iterable[str]) -> Mapping[str, tuple[str, ...]]:
    """Partition a catalog into its deterministic groups.

    Every advertised tool appears in exactly one group; groups preserve the
    fixed GROUP_NAMES order and sort members so equal catalogs always produce
    byte-identical payloads (diagnostics diffing depends on that).
    """
    members: dict[str, list[str]] = {name: [] for name in GROUP_NAMES}
    for internal in internal_names:
        members[tool_group(internal)].append(internal)
    return {
        group: tuple(sorted(names))
        for group, names in members.items()
        if names
    }
