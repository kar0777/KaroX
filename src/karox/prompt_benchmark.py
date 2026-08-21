"""Reproducible prompt-architecture benchmark: mechanics first, no guesses.

Mandate section: prompt A/B benchmark. The full contract is a live
comparison -- RAW harness vs leak-inspired monoliths vs the KaroX
Constitution, same model, same task, same effort -- selected by measured
task success, never by style. Live billing evidence requires provider API
access at benchmark time; this module implements everything that can be
measured honestly WITHOUT a live model, and labels the rest UNAVAILABLE
instead of inventing it:

* prompt bytes a policy sends per turn across a simulated multi-turn
  session whose dynamic context (mode, effort, map digest, memory) keeps
  changing the way a real session's does;
* how much of each turn is a byte-stable prefix a provider could cache
  versus dynamic suffix it cannot;
* prefix churn: how many turns broke the previous turn's prefix.

Numbers are reported raw (bytes, counts). No savings percentages are
fabricated; cost fields carry the MEASURED/UNAVAILABLE label scheme from
the /usage mandate so a reader always knows which class of evidence they
are looking at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .constitution import CONSTITUTION_CORE, compose_system_prompt

PROMPT_BENCHMARK_SCHEMA_VERSION = 1

# One simulated engineering session: the task classes the mandate names,
# each turn with the situational context a real KaroX run would inject.
@dataclass(frozen=True)
class BenchmarkTurn:
    task_class: str
    mode_delta: Optional[str]
    effort_line: Optional[str]
    map_digest: Optional[str]
    memory_context: Optional[str]


DEFAULT_TURNS: Tuple[BenchmarkTurn, ...] = (
    BenchmarkTurn(
        "bug fix",
        "Mode: build.",
        "Effort medium: up to 24 steps.",
        "Map: src/pkg/auth.py, src/pkg/session.py hot.",
        None,
    ),
    BenchmarkTurn(
        "repo exploration",
        "Mode: plan.",
        "Effort low: up to 12 steps.",
        "Map: 42 modules, 3 regions.",
        "Memory: entrypoint is cli.main.",
    ),
    BenchmarkTurn(
        "multi-file implementation",
        "Mode: build.",
        "Effort high: up to 48 steps.",
        "Map: 6 dependent modules on target.",
        "Memory: migrations live in db/.",
    ),
    BenchmarkTurn(
        "architecture",
        "Mode: ideate.",
        "Effort high: up to 48 steps.",
        None,
        "Memory: ADR-12 chose sqlite.",
    ),
    BenchmarkTurn(
        "unknown root cause",
        "Mode: build.",
        "Effort extra-high: up to 72 steps.",
        "Map: stale for current revision.",
        "Memory: flaky test history in ci notes.",
    ),
    BenchmarkTurn(
        "test failure",
        "Mode: build.",
        "Effort medium: up to 24 steps.",
        "Map: tests/test_auth.py co-changes with auth.py.",
        None,
    ),
)


@dataclass(frozen=True)
class PromptPolicy:
    """One way of building the system prompt for every turn.

    ``build`` returns ``(stable_prefix, dynamic_suffix)``: the policy's own
    claim about which part of its prompt is byte-stable. The benchmark
    verifies that claim across turns instead of trusting it -- a policy
    whose "stable" prefix changes between turns is charged with prefix
    churn.
    """

    name: str
    description: str
    build: Callable[[BenchmarkTurn], Tuple[str, str]]


def _raw_policy_build(turn: BenchmarkTurn) -> Tuple[str, str]:
    # The bare harness: one short instruction, everything else inline in
    # whatever order the pieces happened to be added -- no reuse contract.
    parts = ["You are a coding agent. Use tools. Verify your work."]
    for piece in (
        turn.mode_delta,
        turn.effort_line,
        turn.map_digest,
        turn.memory_context,
    ):
        if piece:
            parts.append(piece)
    return "", "\n".join(parts)


def _monolith_policy_build(header: str) -> Callable[[BenchmarkTurn], Tuple[str, str]]:
    # Leak-inspired monoliths: one very large static prompt that carries
    # every behavior all the time, with the situational context appended.
    # The static body is cacheable; its size is the price of admission on
    # every cache miss. Body sizes approximate the published prompts'
    # order of magnitude without copying their text.
    body = header + ("\n" + ("policy detail line. " * 40)) * 60
    def build(turn: BenchmarkTurn) -> Tuple[str, str]:
        dynamic = [
            piece
            for piece in (
                turn.mode_delta,
                turn.effort_line,
                turn.map_digest,
                turn.memory_context,
            )
            if piece
        ]
        return body, "\n" + "\n".join(dynamic) if dynamic else ""
    return build


def _constitution_policy_build(turn: BenchmarkTurn) -> Tuple[str, str]:
    composed = compose_system_prompt(
        provider="generic",
        core=CONSTITUTION_CORE,
        mode_delta=turn.mode_delta,
        effort_line=turn.effort_line,
        project_suffix=turn.map_digest,
        memory_context=turn.memory_context,
    )
    return composed.stable_prefix, composed.dynamic_suffix


def default_policies() -> Tuple[PromptPolicy, ...]:
    return (
        PromptPolicy(
            "raw-harness",
            "minimal instruction, situational text inlined, no reuse contract",
            _raw_policy_build,
        ),
        PromptPolicy(
            "opus-inspired-monolith",
            "large static behavior prompt in the Opus leak's shape",
            _monolith_policy_build("# Monolithic agent behavior prompt"),
        ),
        PromptPolicy(
            "fable-inspired-monolith",
            "large static prompt with memory sections in the Fable shape",
            _monolith_policy_build(
                "# Monolithic agent behavior prompt with memory filing rules"
            ),
        ),
        PromptPolicy(
            "karox-constitution",
            "compact stable core + sparse dynamic deltas (karox.constitution)",
            _constitution_policy_build,
        ),
    )


def run_prompt_benchmark(
    policies: Optional[Sequence[PromptPolicy]] = None,
    turns: Sequence[BenchmarkTurn] = DEFAULT_TURNS,
) -> Dict[str, object]:
    """Measure every policy over the same simulated session.

    Pure computation: deterministic, offline, safe in tests. Returns raw
    numbers plus honest evidence labels; interpretation is the reader's.
    """

    results: List[Dict[str, object]] = []
    for policy in policies if policies is not None else default_policies():
        total_bytes = 0
        stable_bytes = 0
        dynamic_bytes = 0
        prefix_churn = 0
        previous_prefix: Optional[str] = None
        for turn in turns:
            prefix, dynamic = policy.build(turn)
            total_bytes += len(prefix.encode()) + len(dynamic.encode())
            stable_bytes += len(prefix.encode())
            dynamic_bytes += len(dynamic.encode())
            if previous_prefix is not None and prefix != previous_prefix:
                prefix_churn += 1
            previous_prefix = prefix
        # Cacheable reuse: bytes a prefix-caching provider would not have
        # to re-price after the first turn, if and only if the prefix
        # never churned.
        reusable_bytes = 0
        if prefix_churn == 0 and previous_prefix is not None:
            reusable_bytes = len(previous_prefix.encode()) * (len(turns) - 1)
        results.append(
            {
                "policy": policy.name,
                "description": policy.description,
                "turns": len(turns),
                "prompt_bytes_total": total_bytes,
                "stable_prefix_bytes_total": stable_bytes,
                "dynamic_bytes_total": dynamic_bytes,
                "prefix_churn_turns": prefix_churn,
                "reusable_prefix_bytes": reusable_bytes,
            }
        )
    return {
        "schema_version": PROMPT_BENCHMARK_SCHEMA_VERSION,
        "task_classes": [turn.task_class for turn in turns],
        "results": results,
        "evidence": {
            "prompt_mechanics": "MEASURED",
            "live_task_success": "UNAVAILABLE (requires live provider runs)",
            "live_billing": "UNAVAILABLE (requires provider usage records)",
        },
        "selection_rule": (
            "winner selection requires the live comparison: same model, "
            "same task corpus, same effort, judged on task success, "
            "verification success, and false completion -- not style"
        ),
    }
