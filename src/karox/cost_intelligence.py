"""Cost Intelligence — CI-0 through CI-5 (Phase 5).

Measures the primary metric (**accepted-verified-task cost**) and implements
the five cost-reduction layers:

CI-0 Telemetry:
    :class:`UsageRecord` + :class:`CostLedger` — input/output tokens, cache
    hits, provider cost, latency, retries, round trips.

CI-1 Safe fast paths:
    :class:`StablePrefixCache` — provider-native prompt caching with a stable
    prefix so the same system+tools preamble is billed at cache-read rate.
    :class:`ToolSchemaDeduplicator` — identical tool schemas are sent once.
    :class:`ReadCache` — avoids re-reading unchanged files.

CI-2 Reversible compaction:
    :class:`CompactSummary` — typed compact summaries with content hashes,
    raw retrievable artifacts, deterministic expansion, bounded previews,
    and secret redaction.

CI-3 Context lifecycle:
    :class:`ContextWindow` — working context set with pinned facts, recent
    events, retention, expiry, cleanup, and restart recovery.

CI-4 Fewer round trips:
    :class:`BatchPlanner` — grouped tool planning, parallel safe reads,
    verification bundling.

CI-5 Governor:
    :class:`CostGovernor` — soft/hard budgets, warnings, explicit user
    override, no silent model downgrade, reversible decisions, shadow mode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from typing import Any, Optional


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One model-call usage measurement."""

    session_id: str
    step: int
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    accepted: bool = False
    verified: bool = False
    retried: bool = False
    timestamp: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CostLedger:
    """Accumulates usage records and computes the primary metric."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(self, **kwargs: Any) -> UsageRecord:
        kwargs.setdefault("timestamp", time.time())
        rec = UsageRecord(**kwargs)
        self._records.append(rec)
        return rec

    def total_cost(self, session_id: Optional[str] = None) -> float:
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        return sum(r.cost_usd for r in recs)

    def total_tokens(self, session_id: Optional[str] = None) -> int:
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        return sum(r.total_tokens for r in recs)

    def round_trips(self, session_id: Optional[str] = None) -> int:
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        return len(recs)

    def retry_count(self, session_id: Optional[str] = None) -> int:
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        return sum(1 for r in recs if r.retried)

    def cache_hit_rate(self, session_id: Optional[str] = None) -> float:
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        total_input = sum(r.input_tokens for r in recs)
        if total_input == 0:
            return 0.0
        cached = sum(r.cache_read_tokens for r in recs)
        return cached / total_input

    def accepted_verified_task_cost(self, session_id: Optional[str] = None) -> float:
        """The primary metric: cost per accepted-and-verified task."""
        recs = [r for r in self._records if session_id is None or r.session_id == session_id]
        av_tasks = [r for r in recs if r.accepted and r.verified]
        if not av_tasks:
            return 0.0
        total = sum(r.cost_usd for r in av_tasks)
        return total / len(av_tasks)

    def summary(self, session_id: Optional[str] = None) -> dict[str, Any]:
        return {
            "total_cost_usd": self.total_cost(session_id),
            "total_tokens": self.total_tokens(session_id),
            "round_trips": self.round_trips(session_id),
            "retries": self.retry_count(session_id),
            "cache_hit_rate": round(self.cache_hit_rate(session_id), 4),
            "accepted_verified_task_cost": self.accepted_verified_task_cost(session_id),
        }


# --------------------------------------------------------------------------- #
# CI-1: Safe fast paths                                                       #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class CacheKey:
    """A stable cache key derived from the prefix content."""

    key: str
    prefix_hash: str
    token_estimate: int


class StablePrefixCache:
    """CI-1: provider-native prompt caching with a stable prefix.

    The agent resends the whole transcript on every step. The prefix (system
    prompt + tool schemas + early conversation turns) is stable, so the
    provider can bill it at cache-read rate if we send the same ``cache_key``.
    This class computes that key deterministically.
    """

    def __init__(self) -> None:
        self._last_key: Optional[CacheKey] = None

    def compute_key(self, *, system_prompt: str, tool_schemas: str, session_id: str) -> CacheKey:
        prefix = f"{system_prompt}\n{tool_schemas}"
        prefix_hash = hashlib.sha256(prefix.encode()).hexdigest()[:16]
        key = f"karox-session-{session_id}-{prefix_hash}"
        token_est = len(prefix) // 4  # rough estimate; actual is provider-side
        ck = CacheKey(key=key, prefix_hash=prefix_hash, token_estimate=token_est)
        self._last_key = ck
        return ck

    @property
    def last_key(self) -> Optional[CacheKey]:
        return self._last_key


class ToolSchemaDeduplicator:
    """CI-1: send identical tool schemas only once.

    If two providers share the same tool definition, sending it twice doubles
    the input tokens for no value. This deduplicates by a canonical hash of
    the schema.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}  # schema_hash → tool_name
        self._deduped_count = 0

    def deduplicate(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        for tool in tools:
            schema = json.dumps(tool, sort_keys=True)
            schema_hash = hashlib.sha256(schema.encode()).hexdigest()[:16]
            if schema_hash in self._seen:
                self._deduped_count += 1
                continue
            self._seen[schema_hash] = tool.get("name", "")
            unique.append(tool)
        return unique

    @property
    def deduped_count(self) -> int:
        return self._deduped_count


class ReadCache:
    """CI-1: avoid re-reading unchanged files.

    A file that has not been modified since the last read produces the same
    content; sending it again bills at full input-token rate. This cache
    tracks file mtime+size and returns a cache-hit signal instead of the
    content when nothing changed.
    """

    def __init__(self) -> None:
        self._fingerprints: dict[str, tuple[float, int]] = {}
        self._hits = 0
        self._misses = 0

    def check(self, path: str, mtime: float, size: int) -> bool:
        """Return True if the file is unchanged since the last check."""
        fp = (mtime, size)
        if self._fingerprints.get(path) == fp:
            self._hits += 1
            return True
        self._fingerprints[path] = fp
        self._misses += 1
        return False

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses


# --------------------------------------------------------------------------- #
# CI-2: Reversible compaction                                                 #
# --------------------------------------------------------------------------- #


_SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|key|password|api_key|authorization)\s*[:=]\s*\S+"
)


def _redact_text(text: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", text)


@dataclasses.dataclass(frozen=True)
class CompactSummary:
    """CI-2: typed compact summary with reversible expansion.

    When the context window fills, the middle of the conversation is replaced
    by a summary. This summary carries:
    * ``content_hash`` — the hash of the raw content it replaces, so the
      original is retrievable from an artifact store;
    * ``summary`` — the redacted short text;
    * ``token_count`` — how many tokens the summary occupies (vs the original);
    * ``raw_artifact_id`` — where the full raw content lives.
    """

    content_hash: str
    summary: str
    token_count: int
    raw_artifact_id: Optional[str]
    original_token_count: int

    @property
    def savings(self) -> int:
        return max(0, self.original_token_count - self.token_count)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def build_compact_summary(
    *, raw_content: str, summary: str, original_token_count: int,
    raw_artifact_id: Optional[str] = None,
) -> CompactSummary:
    """Build a typed compact summary, redacting secrets from the summary."""
    content_hash = hashlib.sha256(raw_content.encode()).hexdigest()[:16]
    redacted_summary = _redact_text(summary)
    token_count = len(redacted_summary) // 4
    return CompactSummary(
        content_hash=content_hash,
        summary=redacted_summary,
        token_count=token_count,
        raw_artifact_id=raw_artifact_id,
        original_token_count=original_token_count,
    )


# --------------------------------------------------------------------------- #
# CI-3: Context lifecycle                                                     #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class PinnedFact:
    """A fact that must stay in context for the entire session."""
    fact_id: str
    content: str
    pinned_at: float = 0.0


class ContextWindow:
    """CI-3: working context set with lifecycle management.

    Tracks:
    * ``pinned`` — facts that never expire (project paths, identity);
    * ``recent`` — a bounded ring of recent events/messages;
    * ``artifacts`` — retrievable by ID for compaction expansion.

    Old ``recent`` entries are evicted when the ring exceeds ``max_recent``.
    Pinned facts survive eviction.
    """

    def __init__(self, *, max_recent: int = 100) -> None:
        self._pinned: dict[str, PinnedFact] = {}
        self._recent: list[Any] = []
        self._max_recent = max_recent
        self._artifacts: dict[str, str] = {}
        self._evicted = 0

    def pin(self, fact_id: str, content: str) -> None:
        self._pinned[fact_id] = PinnedFact(fact_id=fact_id, content=content, pinned_at=time.time())

    def add_recent(self, item: Any) -> None:
        self._recent.append(item)
        while len(self._recent) > self._max_recent:
            self._recent.pop(0)
            self._evicted += 1

    def store_artifact(self, artifact_id: str, content: str) -> None:
        self._artifacts[artifact_id] = content

    def get_artifact(self, artifact_id: str) -> Optional[str]:
        return self._artifacts.get(artifact_id)

    @property
    def pinned_count(self) -> int:
        return len(self._pinned)

    @property
    def recent_count(self) -> int:
        return len(self._recent)

    @property
    def evicted_count(self) -> int:
        return self._evicted

    def snapshot(self) -> dict[str, Any]:
        return {
            "pinned": len(self._pinned),
            "recent": len(self._recent),
            "artifacts": len(self._artifacts),
            "evicted": self._evicted,
        }


# --------------------------------------------------------------------------- #
# CI-4: Fewer round trips                                                     #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class BatchPlan:
    """CI-4: a plan for executing tool calls in fewer model round trips."""
    parallel_reads: tuple[str, ...]
    sequential_writes: tuple[str, ...]
    bundled_verification: bool
    estimated_round_trips_saved: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class BatchPlanner:
    """CI-4: groups independent read operations into one model round trip.

    When the model requests multiple read-only operations (read_file,
    list_files, search), they can be dispatched in parallel rather than
    sequentially. Writes stay sequential because they may depend on each
    other. Verification is bundled into the last step.
    """

    READ_TOOLS = frozenset({
        "repo.read_file", "repo.read_lines", "repo.list_files",
        "repo.search", "git.status", "git.diff", "git.log",
        "runtime.status", "browser.snapshot", "browser.get_text",
    })

    def plan(self, tool_calls: list[dict[str, Any]]) -> BatchPlan:
        reads: list[str] = []
        writes: list[str] = []
        for call in tool_calls:
            name = call.get("name", "")
            if name in self.READ_TOOLS:
                reads.append(name)
            else:
                writes.append(name)
        # Without batching: each call is its own round trip.
        # With batching: all reads go in one trip.
        saved = max(0, len(reads) - 1) if len(reads) > 1 else 0
        return BatchPlan(
            parallel_reads=tuple(reads),
            sequential_writes=tuple(writes),
            bundled_verification=bool(writes),
            estimated_round_trips_saved=saved,
        )


# --------------------------------------------------------------------------- #
# CI-5: Governor                                                              #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class BudgetDecision:
    """CI-5: the governor's decision on whether to proceed."""
    allowed: bool
    reason: str
    warning: Optional[str]
    override_active: bool
    shadow_mode: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CostGovernor:
    """CI-5: soft/hard budget enforcement with no silent model downgrade.

    * ``soft_budget`` — triggers a warning but allows continuation.
    * ``hard_budget`` — stops the run unless the user has explicitly
      overridden.
    * The governor **never** silently downgrades the model to a cheaper one.
      That decision belongs to the user.
    * ``shadow_mode`` — the governor records what it *would* have done without
      actually enforcing it, so a new budget can be tested safely.
    """

    def __init__(
        self,
        *,
        soft_budget_usd: float = 5.0,
        hard_budget_usd: float = 10.0,
        shadow_mode: bool = False,
    ) -> None:
        self._soft = soft_budget_usd
        self._hard = hard_budget_usd
        self._shadow = shadow_mode
        self._override = False
        self._warnings_issued = 0

    def set_override(self, active: bool) -> None:
        """Explicit user override of the hard budget."""
        self._override = active

    def evaluate(self, *, current_cost_usd: float) -> BudgetDecision:
        warning: Optional[str] = None
        if current_cost_usd >= self._soft:
            warning = (
                f"soft budget ${self._soft:.2f} reached "
                f"(current: ${current_cost_usd:.2f})"
            )
            self._warnings_issued += 1

        if current_cost_usd >= self._hard:
            if self._override:
                return BudgetDecision(
                    allowed=True,
                    reason="hard budget exceeded but override is active",
                    warning=warning,
                    override_active=True,
                    shadow_mode=self._shadow,
                )
            if self._shadow:
                return BudgetDecision(
                    allowed=True,
                    reason=f"shadow: would block at hard budget ${self._hard:.2f}",
                    warning=warning,
                    override_active=False,
                    shadow_mode=True,
                )
            return BudgetDecision(
                allowed=False,
                reason=f"hard budget ${self._hard:.2f} exceeded",
                warning=warning,
                override_active=False,
                shadow_mode=False,
            )
        return BudgetDecision(
            allowed=True,
            reason="within budget",
            warning=warning,
            override_active=self._override,
            shadow_mode=self._shadow,
        )

    @property
    def warnings_issued(self) -> int:
        return self._warnings_issued


__all__ = [
    "BatchPlan",
    "BatchPlanner",
    "BudgetDecision",
    "CacheKey",
    "CompactSummary",
    "ContextWindow",
    "CostGovernor",
    "CostLedger",
    "PinnedFact",
    "ReadCache",
    "StablePrefixCache",
    "ToolSchemaDeduplicator",
    "UsageRecord",
    "build_compact_summary",
]
