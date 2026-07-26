"""Shared support for the Phase 10 hybrid-runtime benchmark suite.

This module deliberately contains **no assertions and no business logic**.  It
only assembles the reusable, deterministic primitives that the
``tests/test_benchmark.py`` gates share:

- ``QueueProvider`` / ``call`` / ``model_response`` — the same scripted provider
  contract the existing agent tests use, so a benchmark exercises the *real*
  agent loop, not a mock.
- ``Timer`` — a ``time.perf_counter`` scope that yields wall latency in
  milliseconds for each benchmark's raw run record.
- ``BenchmarkRecord`` — the structured raw run record (pass/fail, latency,
  usage, cost, evidence kinds, limitations, failure reason) the roadmap calls
  for under "KB-HYBRID-01 through 10 with raw run records, failures, latency,
  usage, cost, evidence, and limitations".
- ``RECORDS`` — a module-level accumulator so the benchmark suite prints one
  summary table at the end and so an aggregation test can assert every gate
  produced a well-formed record.
- ``stdio_mcp`` — a convenience that wires a real ``_mcp_echo_server`` record
  plus a ``McpRuntimeBinding`` into a ``CoreRuntime``, reusing the exact
  plumbing the stdio E2E test uses.

Every helper here is deterministic and requires no external key, network, or
paid account.  Live-service conformance stays opt-in and labelled untested.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from _support import SRC  # noqa: F401 - inserts src on sys.path


# --------------------------------------------------------------------------- #
# Scripted provider primitives (identical contract to the agent test suite).  #
# --------------------------------------------------------------------------- #
from karox.providers import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
)


class QueueProvider:
    """Deterministic in-process provider used by the agent benchmark gates.

    This is the same contract the existing agent tests rely on: scripted
    responses are popped FIFO; raising a ``ProviderError`` exercises failure
    paths.  It implements the real ``Provider.complete`` entrypoint, so the
    benchmark drives the genuine ``AgentKernel`` loop.
    """

    provider_name = "test_provider"

    def __init__(
        self,
        responses: List[Any],
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        self.responses = list(responses)
        self.requests: List[ModelRequest] = []
        self.on_complete = on_complete

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.on_complete is not None:
            self.on_complete()
        if not self.responses:
            raise AssertionError("provider received an unexpected request")
        outcome = self.responses.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


def call(call_id: str, name: str, arguments: object, *, raw: bool = False) -> ToolCall:
    encoded = str(arguments) if raw else json.dumps(arguments, ensure_ascii=False)
    return ToolCall(call_id, name, encoded)


def model_response(
    *calls: ToolCall,
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, int]] = None,
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        usage=usage if usage is not None else {"prompt_tokens": 2, "completion_tokens": 1},
        response_id=None,
    )


def routed_cost(
    *,
    content: str = "ok",
    tool_calls: tuple[ToolCall, ...] = (),
    cost: float = 0.25,
    currency: str = "USD",
    cumulative_cost: float = 1.25,
) -> ModelResponse:
    """A routed-cost ``ModelResponse`` carrying the budget/cost fields.

    Mirrors the shape used by ``test_routed_response_persists_secret_safe_audit``
    so a benchmark can prove cost/usage capture flows end-to-end through the
    session store and the agent report.
    """
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={"prompt_tokens": 4, "completion_tokens": 2},
        route_attempts=(
            {
                "route_index": 0,
                "provider_id": "private-provider",
                "model": "model-a",
                "status": "completed",
            },
        ),
        selected_provider="private-provider",
        selected_model="model-a",
        cost=cost,
        currency=currency,
        pricing_version="2026-07",
        cumulative_usage={"prompt_tokens": 14, "completion_tokens": 6},
        cumulative_cost=cumulative_cost,
    )


# --------------------------------------------------------------------------- #
# Timing + raw run records.                                                   #
# --------------------------------------------------------------------------- #
class Timer:
    """A minimal ``perf_counter`` scope that records wall latency in ms."""

    def __init__(self) -> None:
        self.elapsed_ms: float = 0.0

    @contextmanager
    def measure(self) -> Iterator["Timer"]:
        start = time.perf_counter()
        try:
            yield self
        finally:
            self.elapsed_ms = (time.perf_counter() - start) * 1000.0


@dataclass
class BenchmarkRecord:
    """A structured raw run record for one KB-HYBRID gate."""

    benchmark_id: str
    name: str
    passed: bool
    latency_ms: float
    usage: Dict[str, Any] = field(default_factory=dict)
    cost: Optional[Dict[str, Any]] = None
    evidence_summary: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "name": self.name,
            "passed": self.passed,
            "latency_ms": round(self.latency_ms, 3),
            "usage": self.usage,
            "cost": self.cost,
            "evidence_summary": list(self.evidence_summary),
            "limitations": list(self.limitations),
            "failure_reason": self.failure_reason,
        }


RECORDS: List[BenchmarkRecord] = []


def record(rec: BenchmarkRecord) -> BenchmarkRecord:
    """Append a run record to the module accumulator and return it."""
    RECORDS.append(rec)
    return rec


def reset_records() -> None:
    """Clear the accumulator (used by the benchmark suite's setUp)."""
    RECORDS.clear()


def summary_table() -> str:
    """Render the accumulated records as a plain-text summary table.

    This is the "raw run record" output the roadmap asks for: every benchmark
    run prints it once at the end, so latency/usage/cost/evidence are
    inspectable without committing stale numbers to the repository.
    """
    lines = [
        "",
        "KaroX hybrid runtime benchmark — raw run records",
        "=" * 72,
        f"{'ID':<13} {'Pass':<5} {'Lat(ms)':>8}  {'Usage':<22} {'Cost':<14} Gate",
        "-" * 72,
    ]
    for rec in RECORDS:
        usage = (
            f"req={rec.usage.get('requests', '-')},"
            f"tok={rec.usage.get('prompt_tokens', '-')}/"
            f"{rec.usage.get('completion_tokens', '-')}"
        )
        cost = "-"
        if rec.cost:
            cur = rec.cost.get("currency", "?")
            total = rec.cost.get("cumulative_cost", rec.cost.get("cost", "-"))
            cost = f"{total} {cur}"
        flag = "OK" if rec.passed else "FAIL"
        lines.append(
            f"{rec.benchmark_id:<13} {flag:<5} {rec.latency_ms:>8.1f}  "
            f"{usage:<22} {cost:<14} {rec.name}"
        )
    passed = sum(1 for r in RECORDS if r.passed)
    lines.append("-" * 72)
    lines.append(f"{passed}/{len(RECORDS)} gates passed")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Real stdio MCP wiring (reused from the Phase 5 E2E test plumbing).          #
# --------------------------------------------------------------------------- #
def stdio_mcp(root: Path):
    """Build a real stdio MCP ``McpRegistry`` + record + client + tools.

    Returns ``(registry, record, client, tools)`` against the bundled
    ``tests/_mcp_echo_server.py`` so a benchmark can attach the binding to a
    ``CoreRuntime`` without re-implementing discovery.
    """
    from karox.mcp_client import (
        McpClient,
        McpRegistry,
        McpServerRecord,
    )

    registry = McpRegistry(root / "mcp-servers.json")
    record = McpServerRecord(
        server_id="echo",
        namespace="echo",
        transport="stdio",
        command=sys.executable,
        args=(str(Path(__file__).resolve().parent / "_mcp_echo_server.py"),),
        read_only_tools=("echo",),
        timeout_seconds=15.0,
    )
    registry.put(record)
    client = McpClient(registry)
    tools = client.discover("echo", root)
    return registry, record, client, tools
