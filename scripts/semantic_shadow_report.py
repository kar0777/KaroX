"""Offline report for KaroX semantic-shadow session telemetry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox.semantic_shadow import summarize_shadow_usage  # noqa: E402


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def summarize_payload(payload: object) -> dict[str, Any]:
    """Return a semantic-shadow summary from a saved session/report/usage JSON."""

    root = _mapping(payload)
    if root is None:
        raise ValueError("expected a JSON object")

    usage = _mapping(root.get("usage"))
    if usage is not None:
        return summarize_shadow_usage(usage)

    if isinstance(root.get("events"), list) or isinstance(
        root.get("shadow_provider_failures"), list
    ):
        return summarize_shadow_usage(root)

    project_context = _mapping(root.get("project_context"))
    if project_context is not None:
        existing = _mapping(project_context.get("semantic_shadow"))
        if existing is not None:
            return dict(existing)

    raise ValueError(
        "JSON has no session usage, usage events, or project_context.semantic_shadow"
    )


def _human_lines(path: Path, summary: Mapping[str, Any]) -> list[str]:
    opportunity = _mapping(summary.get("deterministic_transition_candidates")) or {}
    repeated_reads = _mapping(summary.get("repeated_unchanged_reads")) or {}
    repeated_proofs = _mapping(summary.get("repeated_same_state_proofs")) or {}
    lines = [
        str(path),
        f"  model boundaries: {summary.get('main_model_boundaries', 0)}",
        (
            "  deterministic-transition candidates: "
            f"{opportunity.get('boundaries', 0)} "
            f"({opportunity.get('fraction', 0)})"
        ),
        (
            "  candidate tokens: "
            f"prompt={opportunity.get('prompt_tokens', 0)} "
            f"uncached={opportunity.get('uncached_prompt_tokens', 0)} "
            f"completion={opportunity.get('completion_tokens', 0)}"
        ),
        (
            "  repeats: "
            f"reads={repeated_reads.get('boundaries', 0)} "
            f"proofs={repeated_proofs.get('boundaries', 0)}"
        ),
        f"  reasons: {json.dumps(opportunity.get('reason_counts', {}), sort_keys=True)}",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize KaroX semantic-shadow telemetry from saved JSON."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON keyed by input path",
    )
    args = parser.parse_args(argv)

    reports: dict[str, dict[str, Any]] = {}
    failed = False
    for path in args.paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            reports[str(path)] = summarize_payload(payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            failed = True
            reports[str(path)] = {"error": f"{type(exc).__name__}: {exc}"}

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        first = True
        for path_text, summary in reports.items():
            if not first:
                print()
            first = False
            path = Path(path_text)
            if "error" in summary:
                print(path)
                print(f"  error: {summary['error']}")
            else:
                print("\n".join(_human_lines(path, summary)))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
