"""Presentation helpers for the KaroX command surface: argv aliases, JSON/human output, session summaries, pricing arguments, and the streaming observers. No handler owns state here."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from dataclasses import asdict
from typing import Any, Optional, Sequence
from .cli_common import (
    write as _write,
)
from .agent import (
    AgentEvent,
    AgentEventKind,
    AgentReport,
)
from .paths import session_dir
from .registry import (
    ModelPricing,
)


_SHORT_COMMANDS = {
    "models": ("model", "list"),
    "providers": ("provider", "list"),
    "sessions": ("session", "list"),
    "skills": ("skill", "list"),
}
_DEFAULT_LIST_GROUPS = frozenset({"model", "provider", "session", "skill"})


def _normalize_argv(arguments: list[str]) -> list[str]:
    """Accept the short daily forms without a second command tree."""

    if not arguments:
        return arguments
    head = arguments[0]
    short = _SHORT_COMMANDS.get(head)
    if short is not None:
        if len(arguments) == 1:
            return list(short)
        if arguments[1].startswith("-"):
            return [*short, *arguments[1:]]
    if head in _DEFAULT_LIST_GROUPS:
        if len(arguments) == 1:
            return [head, "list"]
        if arguments[1].startswith("-") and arguments[1] not in {"-h", "--help"}:
            return [head, "list", *arguments[1:]]
    return arguments


def _record_summary(record: Any) -> dict[str, Any]:
    usage = record.usage if isinstance(record.usage, dict) else {}
    checks = [item for item in record.checks if isinstance(item, dict)]
    return {
        "session_id": record.session_id,
        "name": record.name,
        "parent_session_id": record.parent_session_id,
        "archived": record.archived,
        "repository": record.repository,
        "branch": record.branch,
        "access_profile": record.access_profile,
        "task": record.task,
        "status": record.status,
        "phase": record.phase,
        "revision": record.revision,
        "updated_at": record.updated_at,
        "revoked": record.revoked,
        "changed_files": len(record.changed_files),
        "checks": len(checks),
        "checks_failed": sum(1 for item in checks if item.get("ok") is False),
        "requests": usage.get("requests"),
        "total_tokens": usage.get("total_tokens"),
        "costs": dict(usage.get("costs") or {}),
    }


def _pairs(values: Sequence[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} must use NAME=VALUE")
        name, item = value.split("=", 1)
        if not name:
            raise ValueError(f"{label} name must not be empty")
        if name in result:
            raise ValueError(f"duplicate {label} name: {name}")
        result[name] = item
    return result


def _pricing(args: argparse.Namespace) -> Optional[ModelPricing]:
    values = (
        getattr(args, "pricing_version", None),
        getattr(args, "currency", None),
        getattr(args, "input_per_million", None),
        getattr(args, "output_per_million", None),
        getattr(args, "pricing_source", None),
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "pricing requires version, currency, input/output prices, and source"
        )
    return ModelPricing(
        args.pricing_version,
        args.currency,
        args.input_per_million,
        args.output_per_million,
        args.pricing_source,
        cache_read_per_million=getattr(args, "cache_read_per_million", None),
        cache_write_per_million=getattr(args, "cache_write_per_million", None),
    )


def _load_pricing_registry() -> Any:
    """Load the versioned pricing document; absence means unknown, not zero."""

    path = session_dir() / "pricing.json"
    try:
        from .provider_pricing import PricingRegistry

        registry = PricingRegistry.from_path(path)
    except ValueError as exc:
        print(f"warning: ignoring invalid pricing document {path}: {exc}", file=sys.stderr)
        return None
    return registry if len(registry) > 0 else None


def _stream_progress() -> Any:
    text_open = [False]
    reasoning = [0]
    last = [time.monotonic()]
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    def emit(text: str) -> None:
        try:
            sys.stderr.write(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stderr, "encoding", None) or "ascii"
            sys.stderr.write(text.encode(encoding, "replace").decode(encoding, "replace"))
        sys.stderr.flush()

    def line(text: str) -> None:
        if text_open[0]:
            emit("\n")
            text_open[0] = False
        emit(text + "\n")

    def observe(event: AgentEvent) -> None:
        if event.kind is AgentEventKind.TEXT_DELTA:
            emit(event.text_delta or "")
            text_open[0] = True
        elif event.kind is AgentEventKind.REASONING_SUMMARY_DELTA:
            emit(event.reasoning_summary_delta or "")
            text_open[0] = True
        elif event.kind is AgentEventKind.REASONING_DELTA:
            reasoning[0] += len(event.reasoning_delta or "")
            now = time.monotonic()
            if now - last[0] >= 15.0:
                last[0] = now
                line(f"  ~ reasoning {reasoning[0]} chars")
        elif event.kind is AgentEventKind.STEP_STARTED:
            line(f"[step {event.step}]")
        elif event.kind is AgentEventKind.COMPACTED:
            line(f"[context: {event.summary}]")
        elif event.kind is AgentEventKind.TOOL_STARTED:
            detail = event.detail or ""
            if "\t" in detail:
                detail = f"{len(detail.splitlines())} items"
            line(f"  -> {event.tool} {detail}".rstrip())
        elif event.kind is AgentEventKind.TOOL_FINISHED:
            mark = "ok" if event.ok else "failed"
            line(f"  <- {event.tool} {mark} ({event.summary})")
        elif event.kind is AgentEventKind.FINISHED:
            line(f"[{event.status}: {event.reason}]")

    return observe


def _stream_json() -> Any:
    def observe(event: AgentEvent) -> None:
        payload = {"type": "event", **asdict(event)}
        _write(json.dumps(payload, ensure_ascii=False) + "\n")

    return observe


def _print_agent_report(report: AgentReport) -> None:
    from .security import redact

    _write(f"status: {report.status}\n")
    _write(f"reason: {report.reason}\n")
    _write(f"steps: {report.steps}\n")
    if report.changed_files:
        _write("changed files: " + ", ".join(report.changed_files) + "\n")
    passed = sum(1 for item in report.checks if item.get("ok") is True)
    if report.checks:
        _write(f"checks: {passed}/{len(report.checks)}\n")
    tokens = report.usage.get("total_tokens")
    if isinstance(tokens, int) and tokens:
        _write(f"tokens: {tokens}\n")
    costs = report.usage.get("costs")
    if isinstance(costs, dict) and costs:
        _write("costs: " + ", ".join(f"{key}={value}" for key, value in costs.items()) + "\n")
    if report.usage.get("cost_status") == "unknown":
        reason = str(report.usage.get("cost_unknown_reason") or "")
        detail = {
            "usage_missing": "the provider did not report usage",
            "no_pricing": "no pricing record for this model",
        }.get(reason, reason or "reason unavailable")
        suffix = "; the measured total is incomplete" if costs else ""
        _write(f"costs: unknown ({detail}){suffix}\n")
    if report.evidence:
        _write(f"evidence records: {len(report.evidence)}\n")
    if report.answer_basis:
        _write(f"answer basis: {len(report.answer_basis)}\n")
    sources = report.project_context.get("sources") if report.project_context else None
    if sources:
        names = [
            str(item.get("path")) if isinstance(item, dict) else str(item)
            for item in sources
        ]
        _write("project instructions: " + ", ".join(names) + "\n")
    if report.compaction:
        _write("context compacted: " + json.dumps(report.compaction, ensure_ascii=False) + "\n")
    if report.restored:
        _write(f"restored: {report.restored}\n")
    if report.unmet:
        _write("unmet: " + "; ".join(report.unmet) + "\n")
    if report.failure_map:
        from .check_report import render_failure_map

        rendered = render_failure_map(report.failure_map)
        if rendered:
            _write(rendered + "\n")
    answer = redact(report.provider_message or "").strip()
    if answer:
        _write("\n" + answer + "\n")
