"""KaroX-level output policy: progressive disclosure per mode (P1.4).

MODEL WORK is not USER NARRATION. A turn produces a structured report (result,
verification, warnings, failures, evidence, artifact references); how much of
it the user sees is a policy decision, not a formatting accident, and not a
system prompt. The renderer is deterministic: the same report in the same mode
always produces the same text.

Modes:

* CONCISE: result line, verification line, warnings; details stay behind the
  artifact reference.
* STANDARD: CONCISE plus bounded evidence lines.
* DETAILED: the full report inline.
* LEARNING: STANDARD plus one short "why" line per action, for users who want
  to learn what the runtime did on their behalf.
* ADAPTIVE (default): CONCISE while everything is green; any failure or
  warning escalates to STANDARD with the failure first, because hiding a
  failure to save characters is never acceptable.

Safety invariant, enforced by tests: failures and warnings survive rendering
in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Sequence


class OutputMode(str, Enum):
    ADAPTIVE = "adaptive"
    CONCISE = "concise"
    STANDARD = "standard"
    DETAILED = "detailed"
    LEARNING = "learning"


_EVIDENCE_LIMIT_STANDARD = 5

_CHECK = "\u2713"
_WARN = "\u26a0"
_CROSS = "\u2717"


@dataclass(frozen=True)
class TurnReport:
    """A structured, renderer-independent account of one finished turn."""

    result: str
    verification: Optional[str] = None
    warnings: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    explanations: tuple[str, ...] = ()
    details_ref: Optional[str] = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TurnReport":
        def _text(key: str) -> Optional[str]:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None

        def _lines(key: str) -> tuple[str, ...]:
            value = payload.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return tuple(str(item) for item in value if str(item))
            return ()

        return cls(
            result=_text("result") or "",
            verification=_text("verification"),
            warnings=_lines("warnings"),
            failures=_lines("failures"),
            evidence=_lines("evidence"),
            explanations=_lines("explanations"),
            details_ref=_text("details_ref"),
        )


def resolve_mode(report: TurnReport, mode: OutputMode) -> OutputMode:
    """ADAPTIVE picks the effective mode from the report itself."""
    if mode is not OutputMode.ADAPTIVE:
        return mode
    if report.failures or report.warnings:
        return OutputMode.STANDARD
    return OutputMode.CONCISE


def render_turn_report(
    report: TurnReport, mode: OutputMode = OutputMode.ADAPTIVE
) -> str:
    """Render one turn report for the user under the given policy mode.

    Failures always render, first, in every mode. Warnings always render in
    every mode. Only evidence and explanations are progressive.
    """
    effective = resolve_mode(report, mode)
    lines: list[str] = []
    for failure in report.failures:
        lines.append(f"{_CROSS} {failure}")
    if report.result:
        lines.append(f"{_CHECK} {report.result}")
    if report.verification:
        lines.append(f"{_CHECK} {report.verification}")
    for warning in report.warnings:
        lines.append(f"{_WARN} {warning}")

    if effective in (OutputMode.STANDARD, OutputMode.LEARNING):
        for item in report.evidence[:_EVIDENCE_LIMIT_STANDARD]:
            lines.append(f"  - {item}")
        hidden = len(report.evidence) - _EVIDENCE_LIMIT_STANDARD
        if hidden > 0:
            lines.append(f"  ({hidden} more in details)")
    elif effective is OutputMode.DETAILED:
        for item in report.evidence:
            lines.append(f"  - {item}")

    if effective in (OutputMode.LEARNING, OutputMode.DETAILED):
        for why in report.explanations:
            lines.append(f"  why: {why}")

    if report.details_ref and effective is not OutputMode.DETAILED:
        lines.append(f"[Details] {report.details_ref}")
    return "\n".join(lines)


@dataclass(frozen=True)
class OutputMeasurement:
    """Characters per mode for one report; the honesty check for economy."""

    chars_by_mode: Mapping[str, int] = field(default_factory=dict)

    @property
    def concise_saving_ratio(self) -> float:
        detailed = self.chars_by_mode.get(OutputMode.DETAILED.value, 0)
        concise = self.chars_by_mode.get(OutputMode.CONCISE.value, 0)
        if detailed <= 0:
            return 0.0
        return 1.0 - (concise / detailed)


def measure_output(report: TurnReport) -> OutputMeasurement:
    """Deterministically measure rendered size in every mode."""
    return OutputMeasurement(
        chars_by_mode={
            mode.value: len(render_turn_report(report, mode))
            for mode in OutputMode
        }
    )
