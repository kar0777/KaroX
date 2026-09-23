"""Controlled Economy OFF-vs-ON evaluation over persisted local measurements.

The mandate's contract for an honest comparison:

* OFF disables economy transformations and nothing else -- model, effort,
  task, tests, verification, and safety are identical by construction,
  and this module *verifies* parity rather than assuming it.
* Every compared number was measured in this process and persisted in the
  session's usage events. A metric one side never wrote is UNAVAILABLE
  for the pair -- it is never zero-filled into a flattering delta.
* No dollars. A local evaluation has no live API bill, so a money column
  would be an invention; costs belong to the live A/B which reports
  provider-measured figures only.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional

from .usage_report import MEASURED, UNAVAILABLE, last_economy_event

_DASH = "—"


@dataclasses.dataclass(frozen=True)
class MetricDelta:
    """One measured OFF/ON pair; UNAVAILABLE unless both sides were written."""

    name: str
    off: Optional[int]
    on: Optional[int]

    @property
    def label(self) -> str:
        return MEASURED if self.off is not None and self.on is not None else UNAVAILABLE

    def render(self) -> str:
        off_text = _DASH if self.off is None else str(self.off)
        on_text = _DASH if self.on is None else str(self.on)
        return f"{self.name}: OFF {off_text} / ON {on_text} · {self.label}"


@dataclasses.dataclass(frozen=True)
class EconomyComparison:
    """Quality parity verdict plus the measured metric deltas."""

    quality_parity: bool
    parity_issues: tuple[str, ...]
    metrics: tuple[MetricDelta, ...]

    def render(self) -> list[str]:
        lines = [
            "ECONOMY OFF vs ON — local controlled evaluation",
            (
                "quality parity: PASS — identical outcome, answer, and "
                "verification"
                if self.quality_parity
                else "quality parity: FAIL — economy optimization is rejected"
            ),
        ]
        lines.extend(f"  parity issue: {issue}" for issue in self.parity_issues)
        lines.extend("  " + metric.render() for metric in self.metrics)
        lines.append(
            "  cost: not measured locally — live provider billing only "
            "(no invented dollars)"
        )
        return lines


#: The persisted counters an OFF/ON comparison is allowed to read. Names map
#: mandate bullets to the kernel's economy_* usage keys.
_METRIC_KEYS: tuple[tuple[str, str], ...] = (
    ("context chars in", "economy_context_chars_in"),
    ("context chars out", "economy_context_chars_out"),
    ("context items included", "economy_context_included"),
    ("context items referenced", "economy_context_referenced"),
    ("context items elided stale", "economy_context_elided_stale"),
    ("tool schemas included", "economy_tool_schemas_included"),
    ("tool schemas omitted", "economy_tool_schemas_omitted"),
    ("tool schema bytes avoided", "economy_tool_schema_bytes_avoided"),
    ("tool schema bytes saved net", "economy_tool_schema_bytes_saved_net"),
    ("duplicate context chars reused", "economy_reused_chars"),
    ("stale chars superseded", "economy_stale_chars"),
    ("read cache hits", "economy_read_cache_hits"),
    ("ToolVM batched turns avoided", "economy_batched_turns_avoided"),
    ("continuity statements carried", "economy_continuity_statements"),
    ("prefix stable steps", "economy_prefix_stable_steps"),
)


def _metric_value(economy: Mapping[str, Any], key: str) -> Optional[int]:
    value = economy.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclasses.dataclass(frozen=True)
class RunOutcome:
    """The quality-bearing facts of one run, extracted by the caller.

    ``answer`` is the final assistant text, ``outcome`` the report outcome
    string, ``files_changed`` the sorted repository paths the run touched,
    ``verified`` whether verification passed. Wall time is measured by the
    caller's clock; ``None`` when the caller did not time the run.
    """

    answer: str
    outcome: str
    files_changed: tuple[str, ...]
    verified: bool
    wall_time_seconds: Optional[float] = None


def compare_runs(
    *,
    off_usage: Mapping[str, Any],
    on_usage: Mapping[str, Any],
    off_outcome: RunOutcome,
    on_outcome: RunOutcome,
) -> EconomyComparison:
    """Compare two runs of the same task with economy OFF and ON.

    Parity failures are reported, never patched over: a comparison whose
    answers differ is a failed optimization by mandate, regardless of how
    many bytes the ON side saved.
    """

    issues: list[str] = []
    if off_outcome.outcome != on_outcome.outcome:
        issues.append(
            f"outcome differs: OFF {off_outcome.outcome!r} vs ON {on_outcome.outcome!r}"
        )
    if off_outcome.answer != on_outcome.answer:
        issues.append("final answer text differs")
    if off_outcome.files_changed != on_outcome.files_changed:
        issues.append(
            "changed files differ: "
            f"OFF {list(off_outcome.files_changed)} vs ON {list(on_outcome.files_changed)}"
        )
    if off_outcome.verified != on_outcome.verified:
        issues.append(
            f"verification differs: OFF {off_outcome.verified} vs ON {on_outcome.verified}"
        )

    off_economy = last_economy_event(off_usage)
    on_economy = last_economy_event(on_usage)
    metrics = [
        MetricDelta(
            name=name,
            off=_metric_value(off_economy, key),
            on=_metric_value(on_economy, key),
        )
        for name, key in _METRIC_KEYS
    ]
    if (
        off_outcome.wall_time_seconds is not None
        and on_outcome.wall_time_seconds is not None
    ):
        metrics.append(
            MetricDelta(
                name="wall time (ms)",
                off=int(off_outcome.wall_time_seconds * 1000),
                on=int(on_outcome.wall_time_seconds * 1000),
            )
        )
    return EconomyComparison(
        quality_parity=not issues,
        parity_issues=tuple(issues),
        metrics=tuple(metrics),
    )


__all__ = [
    "EconomyComparison",
    "MetricDelta",
    "RunOutcome",
    "compare_runs",
]
