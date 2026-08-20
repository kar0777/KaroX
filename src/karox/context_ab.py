"""Deterministic A/B measurement for KaroX project-context strategies.

The harness measures navigation quality, not model intelligence.  Each case names
files that a good context layer should surface.  KaroX then builds the same task
with Recursive Context off and on using isolated session caches and reports
precision/recall, latency and prompt-size deltas.

A model-backed research subagent can be evaluated separately with
:class:`karox.research_subagent.ResearchSubagent`; keeping the deterministic
context A/B independent means regressions can run in CI without paid API calls.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .project_context import ProjectContext, discover_project_context


@dataclass(frozen=True)
class ContextBenchmarkCase:
    name: str
    goal: str
    expected_implementation: tuple[str, ...] = ()
    expected_tests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("context benchmark case name must be non-empty")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("context benchmark case goal must be non-empty")


@dataclass(frozen=True)
class PathScore:
    expected: int
    predicted: int
    hits: int
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]


@dataclass(frozen=True)
class ContextModeMeasurement:
    mode: str
    duration_ms: float
    prompt_chars: int
    cache_hit: bool
    implementation: tuple[str, ...]
    tests: tuple[str, ...]
    recursive_enabled: bool
    recursive_edge_count: int
    implementation_score: PathScore
    test_score: PathScore


@dataclass(frozen=True)
class ContextABComparison:
    case: str
    goal: str
    root: ContextModeMeasurement
    recursive: ContextModeMeasurement
    added_implementation: tuple[str, ...]
    added_tests: tuple[str, ...]
    duration_delta_ms: float
    prompt_delta_chars: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextABSummary:
    cases: int
    recursive_enabled_cases: int
    implementation_hits_delta: int
    test_hits_delta: int
    mean_implementation_f1_delta: float
    mean_test_f1_delta: float
    quality_wins: int
    quality_losses: int
    mean_duration_delta_ms: float
    mean_prompt_delta_chars: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unique_paths(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in result:
            continue
        result.append(value)
    return tuple(result)


def _score(expected: tuple[str, ...], predicted: tuple[str, ...]) -> PathScore:
    expected_set = set(expected)
    predicted_set = set(predicted)
    hits = len(expected_set.intersection(predicted_set))
    precision = hits / len(predicted_set) if predicted_set else None
    recall = hits / len(expected_set) if expected_set else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None if precision is None or recall is None else 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return PathScore(
        expected=len(expected_set),
        predicted=len(predicted_set),
        hits=hits,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def _paths(context: ProjectContext) -> tuple[tuple[str, ...], tuple[str, ...], bool, int]:
    metadata = context.project_map_metadata or {}
    root_implementation = _unique_paths(metadata.get("implementation", ()))
    root_tests = _unique_paths(metadata.get("tests", ()))
    raw_recursive = metadata.get("recursive")
    recursive: dict[str, Any] = raw_recursive if isinstance(raw_recursive, dict) else {}
    implementation = _unique_paths(
        (*root_implementation, *_unique_paths(recursive.get("additional_implementation", ())))
    )
    tests = _unique_paths((*root_tests, *_unique_paths(recursive.get("additional_tests", ()))))
    edge_count = recursive.get("edge_count")
    return (
        implementation,
        tests,
        bool(recursive.get("enabled")),
        edge_count if isinstance(edge_count, int) else 0,
    )


def _session_id(prefix: str, case: ContextBenchmarkCase, mode: str) -> str:
    digest = hashlib.sha256(
        f"{case.name}\0{case.goal}\0{mode}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix)[:48]
    return f"{clean or 'context-ab'}-{mode}-{digest}"


def _measure(
    repository: Path,
    case: ContextBenchmarkCase,
    *,
    mode: str,
    session_prefix: str,
) -> ContextModeMeasurement:
    started = time.perf_counter()
    context = discover_project_context(
        repository,
        goal=case.goal,
        session_id=_session_id(session_prefix, case, mode),
        recursive_context=mode,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    implementation, tests, recursive_enabled, edge_count = _paths(context)
    return ContextModeMeasurement(
        mode=mode,
        duration_ms=duration_ms,
        prompt_chars=len(context.prompt_suffix),
        cache_hit=bool((context.project_map_metadata or {}).get("cache_hit")),
        implementation=implementation,
        tests=tests,
        recursive_enabled=recursive_enabled,
        recursive_edge_count=edge_count,
        implementation_score=_score(case.expected_implementation, implementation),
        test_score=_score(case.expected_tests, tests),
    )


def compare_context_case(
    repository: Path,
    case: ContextBenchmarkCase,
    *,
    session_prefix: str = "context-ab",
) -> ContextABComparison:
    """Cold-cache comparison of root-only vs dependency-graph depth=1 context."""
    root = repository.expanduser().resolve(strict=True)
    # One comparison should be cold even when the same case is repeated in the
    # same process. RepositoryContextEngine caches are session-scoped, so a
    # per-run suffix prevents yesterday's measurement from making today's second
    # arm look artificially free.
    run_prefix = f"{session_prefix}-{time.time_ns()}"
    baseline = _measure(root, case, mode="off", session_prefix=run_prefix)
    recursive = _measure(root, case, mode="on", session_prefix=run_prefix)
    return ContextABComparison(
        case=case.name,
        goal=case.goal,
        root=baseline,
        recursive=recursive,
        added_implementation=tuple(
            path for path in recursive.implementation if path not in baseline.implementation
        ),
        added_tests=tuple(path for path in recursive.tests if path not in baseline.tests),
        duration_delta_ms=round(recursive.duration_ms - baseline.duration_ms, 3),
        prompt_delta_chars=recursive.prompt_chars - baseline.prompt_chars,
    )


def run_context_ab(
    repository: Path,
    cases: Iterable[ContextBenchmarkCase],
    *,
    session_prefix: str = "context-ab",
) -> tuple[ContextABComparison, ...]:
    """Run a deterministic suite without provider/API calls."""
    return tuple(
        compare_context_case(repository, case, session_prefix=session_prefix)
        for case in cases
    )


def summarize_context_ab(results: Iterable[ContextABComparison]) -> ContextABSummary:
    items = tuple(results)
    if not items:
        return ContextABSummary(
            cases=0,
            recursive_enabled_cases=0,
            implementation_hits_delta=0,
            test_hits_delta=0,
            mean_implementation_f1_delta=0.0,
            mean_test_f1_delta=0.0,
            quality_wins=0,
            quality_losses=0,
            mean_duration_delta_ms=0.0,
            mean_prompt_delta_chars=0.0,
        )
    implementation_delta = sum(
        item.recursive.implementation_score.hits - item.root.implementation_score.hits
        for item in items
    )
    test_delta = sum(
        item.recursive.test_score.hits - item.root.test_score.hits for item in items
    )

    def score_delta(item: ContextABComparison, attribute: str) -> Optional[float]:
        root_score = getattr(item.root, attribute)
        recursive_score = getattr(item.recursive, attribute)
        if root_score.expected <= 0 or recursive_score.expected <= 0:
            return None
        if root_score.f1 is None or recursive_score.f1 is None:
            return None
        return recursive_score.f1 - root_score.f1

    implementation_f1_deltas = [
        value
        for item in items
        if (value := score_delta(item, "implementation_score")) is not None
    ]
    test_f1_deltas = [
        value
        for item in items
        if (value := score_delta(item, "test_score")) is not None
    ]

    quality_wins = 0
    quality_losses = 0
    for item in items:
        deltas = [
            value
            for attribute in ("implementation_score", "test_score")
            if (value := score_delta(item, attribute)) is not None
        ]
        if not deltas:
            continue
        combined = sum(deltas) / len(deltas)
        if combined > 1e-9:
            quality_wins += 1
        elif combined < -1e-9:
            quality_losses += 1

    return ContextABSummary(
        cases=len(items),
        recursive_enabled_cases=sum(item.recursive.recursive_enabled for item in items),
        implementation_hits_delta=implementation_delta,
        test_hits_delta=test_delta,
        mean_implementation_f1_delta=round(
            sum(implementation_f1_deltas) / len(implementation_f1_deltas), 6
        ) if implementation_f1_deltas else 0.0,
        mean_test_f1_delta=round(
            sum(test_f1_deltas) / len(test_f1_deltas), 6
        ) if test_f1_deltas else 0.0,
        quality_wins=quality_wins,
        quality_losses=quality_losses,
        mean_duration_delta_ms=round(
            sum(item.duration_delta_ms for item in items) / len(items), 3
        ),
        mean_prompt_delta_chars=round(
            sum(item.prompt_delta_chars for item in items) / len(items), 3
        ),
    )
