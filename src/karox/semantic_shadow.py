"""Prospective, non-interfering semantic-boundary measurement.

This module is intentionally independent from karox.agent. It observes state
the AgentKernel already had before a provider request and records a conservative
counterfactual: could the runtime certainly have advanced without another model
call?

Nothing here executes an alternative action. UNKNOWN is a first-class result
and is deliberately preferred over optimistic mechanical labels.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


SHADOW_VERSION = "phase0a-v0"


class ShadowWouldCall(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ShadowAuthority(str, Enum):
    RUNTIME = "runtime"
    SEMANTIC = "semantic"
    USER = "user"
    POLICY = "policy"
    UNKNOWN = "unknown"


class ShadowConfidence(str, Enum):
    CERTAIN_MECHANICAL = "certain_mechanical"
    LIKELY_MECHANICAL = "likely_mechanical"
    SEMANTIC_REQUIRED = "semantic_required"
    UNKNOWN = "unknown"


class ShadowReason(str, Enum):
    COMPLETION_ALREADY_VERIFIED = "completion_already_verified"
    ANSWER_ALREADY_COMPLETE = "answer_already_complete"
    VISUAL_REVIEW_REQUIRED = "visual_review_required"
    RUNTIME_TRANSITION_AVAILABLE = "runtime_transition_available"
    FIRST_SEMANTIC_DECISION = "first_semantic_decision"
    ACTIVE_FAILURE = "active_failure"
    UNCLASSIFIED_STATE = "unclassified_state"


class ShadowEffectKind(str, Enum):
    SEMANTIC_CHANGE = "semantic_change"
    OBSERVATION_ONLY = "observation_only"
    PROOF_ONLY = "proof_only"
    PROTOCOL_RECOVERY = "protocol_recovery"
    REPORT_ONLY = "report_only"
    NARRATIVE_ONLY = "narrative_only"
    NO_PROGRESS = "no_progress"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ShadowSnapshot:
    """Bounded state visible before one main AgentKernel provider call."""

    step: int
    session_revision: int
    phase: str
    status: str
    repo_fingerprint: str
    branch: str
    changed_files: tuple[str, ...]
    requirements_digest: str
    active_requirement_ids: tuple[str, ...]
    active_requirement_bytes: int
    provider_history_entries: int
    request_message_count: int
    request_chars: int
    request_dynamic_chars: int
    advertised_tool_count: int
    advertised_tool_schema_bytes: int
    checks_count: int
    failures_count: int
    evidence_count: int
    jobs_count: int
    unfinished_actions_count: int
    plan_todo_count: int
    plan_doing_count: int
    plan_done_count: int
    verified_snapshot_present: bool
    verification_complete: bool
    answer_complete: bool
    awaiting_visual_review: bool
    changed_once: bool
    require_change: bool
    # Exact deterministic work AgentKernel could already name before the model
    # response. These are observations of current runtime capability, not a
    # prediction inferred from what the model later chose to do.
    runtime_transitions: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ShadowDecision:
    """Prospective decision made without access to the current model response."""

    step: int
    snapshot_sha256: str
    would_call_model: ShadowWouldCall
    authority_gap: ShadowAuthority
    confidence: ShadowConfidence
    reason_codes: tuple[ShadowReason, ...]
    available_runtime_transitions: tuple[str, ...] = ()

    def usage_fields(self) -> dict[str, Any]:
        return {
            "shadow_version": SHADOW_VERSION,
            "shadow_would_call_model": self.would_call_model.value,
            "shadow_authority_gap": self.authority_gap.value,
            "shadow_confidence": self.confidence.value,
            "shadow_reason_codes": ",".join(item.value for item in self.reason_codes),
            "shadow_snapshot_sha256": self.snapshot_sha256,
            "shadow_runtime_transitions": ",".join(self.available_runtime_transitions),
        }


@dataclass(frozen=True)
class ShadowStepEffect:
    """Observable effect of the real step, used only for post-call audit."""

    kind: ShadowEffectKind
    tool_calls: int
    read_calls: int
    mutation_calls: int
    check_calls: int
    tool_error_count: int
    changed: bool
    new_evidence_count: int
    generated_argument_bytes: int
    mutation_argument_bytes: int
    provider_text_bytes: int
    tool_names: tuple[str, ...]
    error_types: tuple[str, ...]
    # Hashed call+result identities let the offline profiler prove that a read
    # or proof operation was repeated with the same observable result, without
    # persisting raw file contents or tool arguments in a second telemetry path.
    read_result_signatures: tuple[str, ...] = ()
    proof_result_signatures: tuple[str, ...] = ()

    def usage_fields(self) -> dict[str, Any]:
        return {
            "shadow_effect_kind": self.kind.value,
            "shadow_tool_calls": self.tool_calls,
            "shadow_read_calls": self.read_calls,
            "shadow_mutation_calls": self.mutation_calls,
            "shadow_check_calls": self.check_calls,
            "shadow_tool_error_count": self.tool_error_count,
            "shadow_repository_changed": self.changed,
            "shadow_new_evidence_count": self.new_evidence_count,
            "shadow_generated_argument_bytes": self.generated_argument_bytes,
            "shadow_mutation_argument_bytes": self.mutation_argument_bytes,
            "shadow_provider_text_bytes": self.provider_text_bytes,
            "shadow_tool_names": ",".join(self.tool_names),
            "shadow_error_types": ",".join(self.error_types),
            "shadow_read_result_signatures": ",".join(self.read_result_signatures),
            "shadow_proof_result_signatures": ",".join(self.proof_result_signatures),
        }


def classify_before_model(snapshot: ShadowSnapshot) -> ShadowDecision:
    """Return a conservative pre-call authority decision.

    The first version intentionally has very few NO paths. A false mechanical
    label would encourage suppressing a model call that contained necessary
    semantic work; an UNKNOWN only understates possible savings.
    """

    sha = snapshot.fingerprint()
    if snapshot.awaiting_visual_review:
        return ShadowDecision(
            step=snapshot.step,
            snapshot_sha256=sha,
            would_call_model=ShadowWouldCall.YES,
            authority_gap=ShadowAuthority.SEMANTIC,
            confidence=ShadowConfidence.SEMANTIC_REQUIRED,
            reason_codes=(ShadowReason.VISUAL_REVIEW_REQUIRED,),
        )
    if snapshot.verification_complete:
        return ShadowDecision(
            step=snapshot.step,
            snapshot_sha256=sha,
            would_call_model=ShadowWouldCall.NO,
            authority_gap=ShadowAuthority.RUNTIME,
            confidence=ShadowConfidence.CERTAIN_MECHANICAL,
            reason_codes=(ShadowReason.COMPLETION_ALREADY_VERIFIED,),
            available_runtime_transitions=("finish_from_verified_state",),
        )
    if snapshot.answer_complete:
        return ShadowDecision(
            step=snapshot.step,
            snapshot_sha256=sha,
            would_call_model=ShadowWouldCall.NO,
            authority_gap=ShadowAuthority.RUNTIME,
            confidence=ShadowConfidence.CERTAIN_MECHANICAL,
            reason_codes=(ShadowReason.ANSWER_ALREADY_COMPLETE,),
            available_runtime_transitions=("finish_from_answer_state",),
        )
    if snapshot.runtime_transitions:
        return ShadowDecision(
            step=snapshot.step,
            snapshot_sha256=sha,
            would_call_model=ShadowWouldCall.NO,
            authority_gap=ShadowAuthority.RUNTIME,
            confidence=ShadowConfidence.CERTAIN_MECHANICAL,
            reason_codes=(ShadowReason.RUNTIME_TRANSITION_AVAILABLE,),
            available_runtime_transitions=snapshot.runtime_transitions,
        )
    if snapshot.step <= 1:
        return ShadowDecision(
            step=snapshot.step,
            snapshot_sha256=sha,
            would_call_model=ShadowWouldCall.YES,
            authority_gap=ShadowAuthority.SEMANTIC,
            confidence=ShadowConfidence.SEMANTIC_REQUIRED,
            reason_codes=(ShadowReason.FIRST_SEMANTIC_DECISION,),
        )
    return ShadowDecision(
        step=snapshot.step,
        snapshot_sha256=sha,
        would_call_model=ShadowWouldCall.UNKNOWN,
        authority_gap=ShadowAuthority.UNKNOWN,
        confidence=ShadowConfidence.UNKNOWN,
        reason_codes=(ShadowReason.UNCLASSIFIED_STATE,),
    )


_READ_PREFIXES = (
    "repo.read",
    "repo.search",
    "repo.list",
    "git.log",
    "git.show",
    "git.branch",
)
_PROOF_TOOLS = frozenset({"checks.run", "git.status", "git.diff"})
_MUTATION_TOOLS = frozenset({"repo.write_file", "repo.edit_file"})
_PROTOCOL_ERRORS = frozenset(
    {
        "malformed_arguments",
        "unknown_tool",
        "repeated_action",
    }
)


def _argument_bytes(assistant_entry: Mapping[str, Any]) -> tuple[int, int]:
    total = 0
    mutation = 0
    calls = assistant_entry.get("tool_calls")
    if not isinstance(calls, list):
        return 0, 0
    for raw in calls:
        if not isinstance(raw, Mapping):
            continue
        arguments = raw.get("raw_arguments")
        size = len(arguments.encode("utf-8")) if isinstance(arguments, str) else 0
        total += size
        name = str(raw.get("name") or "")
        if name in _MUTATION_TOOLS or name in {"repo_write_file", "repo_edit_file"}:
            mutation += size
    return total, mutation


def _call_arguments_by_id(
    assistants: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    calls: dict[str, str] = {}
    for assistant in assistants:
        raw_calls = assistant.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw in raw_calls:
            if not isinstance(raw, Mapping):
                continue
            call_id = str(raw.get("call_id") or "")
            arguments = raw.get("raw_arguments")
            if call_id and isinstance(arguments, str):
                calls[call_id] = arguments
    return calls


def _stable_result_signature(
    *,
    core_name: str,
    raw_arguments: str,
    data: Mapping[str, Any],
    proof: bool,
) -> str:
    """Hash stable observable state for one read/proof operation.

    If the tool result exposes too little state to distinguish two executions,
    return an empty string instead of claiming an unchanged repeat.
    """

    keys: tuple[str, ...]
    if proof:
        keys = (
            "argv",
            "completion_identity",
            "exit_code",
            "timed_out",
            "stdout_sha256",
            "stderr_sha256",
            "verdict",
            "completion_target",
            "clean",
            "porcelain",
        )
    else:
        keys = (
            "path",
            "content_sha256",
            "sha256",
            "start",
            "count",
            "total_lines",
            "lines",
            "content",
            "matches",
            "files",
            "entries",
            "truncated",
        )
    stable = {key: data[key] for key in keys if key in data}
    identity_keys = set(stable).difference({"path", "argv", "start", "count"})
    if not identity_keys:
        return ""
    payload = json.dumps(
        {
            "tool": core_name,
            "arguments": raw_arguments,
            "result": stable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def audit_history_delta(
    *,
    snapshot: ShadowSnapshot,
    new_entries: Sequence[Mapping[str, Any]],
    finish_reason: str | None = None,
) -> ShadowStepEffect:
    """Describe what the real step did without changing the pre-call decision."""

    assistants = [item for item in new_entries if item.get("role") == "assistant"]
    tools = [item for item in new_entries if item.get("role") == "tool"]
    arguments_by_id = _call_arguments_by_id(assistants)

    provider_text_bytes = 0
    generated_argument_bytes = 0
    mutation_argument_bytes = 0
    for entry in assistants:
        content = entry.get("content")
        if isinstance(content, str):
            provider_text_bytes += len(content.encode("utf-8"))
        generated, mutating = _argument_bytes(entry)
        generated_argument_bytes += generated
        mutation_argument_bytes += mutating

    tool_names: list[str] = []
    error_types: list[str] = []
    read_calls = 0
    mutation_calls = 0
    check_calls = 0
    tool_error_count = 0
    changed = False
    evidence_ids: set[str] = set()
    read_result_signatures: list[str] = []
    proof_result_signatures: list[str] = []

    for entry in tools:
        core_name = str(entry.get("core_name") or entry.get("tool_name") or "")
        if core_name:
            tool_names.append(core_name)
        error = entry.get("error")
        if isinstance(error, Mapping):
            tool_error_count += 1
            error_type = str(error.get("type") or "")
            if error_type:
                error_types.append(error_type)

        result = entry.get("result")
        if not isinstance(result, Mapping):
            continue
        raw_data = result.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        call_id = str(entry.get("tool_call_id") or "")
        raw_arguments = arguments_by_id.get(call_id, "")
        if result.get("mutation") is True or (
            core_name in _MUTATION_TOOLS and data.get("changed") is True
        ):
            mutation_calls += 1
            changed = changed or data.get("changed") is True
        elif core_name in _PROOF_TOOLS:
            check_calls += 1
            signature = _stable_result_signature(
                core_name=core_name,
                raw_arguments=raw_arguments,
                data=data,
                proof=True,
            )
            if signature:
                proof_result_signatures.append(signature)
        elif core_name.startswith(_READ_PREFIXES):
            read_calls += 1
            signature = _stable_result_signature(
                core_name=core_name,
                raw_arguments=raw_arguments,
                data=data,
                proof=False,
            )
            if signature:
                read_result_signatures.append(signature)

        evidence = result.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                identifier = item.get("evidence_id")
                if isinstance(identifier, str) and identifier:
                    evidence_ids.add(identifier)

    names = set(tool_names)
    if mutation_calls:
        kind = (
            ShadowEffectKind.SEMANTIC_CHANGE
            if len(names.difference(_MUTATION_TOOLS)) == 0
            else ShadowEffectKind.MIXED
        )
    elif tools and tool_error_count == len(tools) and error_types and all(
        item in _PROTOCOL_ERRORS for item in error_types
    ):
        kind = ShadowEffectKind.PROTOCOL_RECOVERY
    elif tools and names and names.issubset(_PROOF_TOOLS):
        kind = ShadowEffectKind.PROOF_ONLY
    elif tools and read_calls == len(tools):
        kind = ShadowEffectKind.OBSERVATION_ONLY
    elif tools:
        kind = ShadowEffectKind.MIXED
    elif snapshot.verification_complete or snapshot.answer_complete:
        kind = ShadowEffectKind.REPORT_ONLY
    elif finish_reason == "length" or provider_text_bytes == 0:
        kind = ShadowEffectKind.NO_PROGRESS
    elif provider_text_bytes:
        kind = ShadowEffectKind.NARRATIVE_ONLY
    else:
        kind = ShadowEffectKind.UNKNOWN

    return ShadowStepEffect(
        kind=kind,
        tool_calls=len(tools),
        read_calls=read_calls,
        mutation_calls=mutation_calls,
        check_calls=check_calls,
        tool_error_count=tool_error_count,
        changed=changed,
        new_evidence_count=len(evidence_ids),
        generated_argument_bytes=generated_argument_bytes,
        mutation_argument_bytes=mutation_argument_bytes,
        provider_text_bytes=provider_text_bytes,
        tool_names=tuple(tool_names),
        error_types=tuple(error_types),
        read_result_signatures=tuple(read_result_signatures),
        proof_result_signatures=tuple(proof_result_signatures),
    )


def request_char_counts(messages: Sequence[Any]) -> tuple[int, int]:
    """Return total and non-system text characters without tokenization."""

    total = 0
    dynamic = 0
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        size = len(content)
        total += size
        if getattr(message, "role", None) != "system":
            dynamic += size
    return total, dynamic


def summarize_shadow_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate Phase 0 annotations without treating them as realized savings."""

    raw_events = usage.get("events")
    events = (
        [item for item in raw_events if isinstance(item, Mapping)]
        if isinstance(raw_events, list)
        else []
    )
    shadow_events = [item for item in events if item.get("shadow_version") == SHADOW_VERSION]
    raw_failures = usage.get("shadow_provider_failures")
    failure_events = (
        [
            item
            for item in raw_failures
            if isinstance(item, Mapping) and item.get("shadow_version") == SHADOW_VERSION
        ]
        if isinstance(raw_failures, list)
        else []
    )
    decision_events = [*shadow_events, *failure_events]
    decisions = {item.value: 0 for item in ShadowWouldCall}
    confidence: dict[str, int] = {}
    effects: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    certain_mechanical = 0
    request_chars = dynamic_chars = 0
    prompt_tokens = completion_tokens = cache_read_tokens = 0

    cost_by_effect: dict[str, dict[str, int]] = {}
    top_costly_boundaries: list[dict[str, Any]] = []
    candidate_boundaries: set[int] = set()
    candidate_reason_counts: dict[str, int] = {}
    candidate_prompt_tokens = 0
    candidate_completion_tokens = 0
    candidate_cache_read_tokens = 0
    seen_read_signatures: set[str] = set()
    seen_proof_signatures: set[str] = set()
    repeated_read_boundaries = repeated_read_calls = 0
    repeated_proof_boundaries = repeated_proof_calls = 0
    tool_error_boundaries = 0

    def split_signatures(event: Mapping[str, Any], field: str) -> tuple[str, ...]:
        return tuple(item for item in str(event.get(field) or "").split(",") if item)

    def add_candidate_reason(reason: str) -> None:
        candidate_reason_counts[reason] = candidate_reason_counts.get(reason, 0) + 1

    for event_index, event in enumerate(decision_events):
        would = str(event.get("shadow_would_call_model") or "")
        if would in decisions:
            decisions[would] += 1
        conf = str(event.get("shadow_confidence") or "")
        if conf:
            confidence[conf] = confidence.get(conf, 0) + 1
        if (
            conf == ShadowConfidence.CERTAIN_MECHANICAL.value
            and would == ShadowWouldCall.NO.value
        ):
            certain_mechanical += 1
        effect = str(event.get("shadow_effect_kind") or "")
        if effect:
            effects[effect] = effects.get(effect, 0) + 1
        for reason in str(event.get("shadow_reason_codes") or "").split(","):
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

        def count(name: str) -> int:
            value = event.get(name)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

        request_chars += count("shadow_request_chars")
        dynamic_chars += count("shadow_dynamic_chars")
        event_prompt = count("prompt_tokens")
        event_completion = count("completion_tokens")
        event_cache = count("cache_read_tokens")
        prompt_tokens += event_prompt
        completion_tokens += event_completion
        cache_read_tokens += event_cache

        if effect:
            bucket = cost_by_effect.setdefault(
                effect,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "cache_read_tokens": 0,
                    "completion_tokens": 0,
                },
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += event_prompt
            bucket["cache_read_tokens"] += event_cache
            bucket["completion_tokens"] += event_completion

        repeated_read_here = 0
        for signature in split_signatures(event, "shadow_read_result_signatures"):
            if signature in seen_read_signatures:
                repeated_read_here += 1
            seen_read_signatures.add(signature)
        if repeated_read_here:
            repeated_read_boundaries += 1
            repeated_read_calls += repeated_read_here

        repeated_proof_here = 0
        for signature in split_signatures(event, "shadow_proof_result_signatures"):
            if signature in seen_proof_signatures:
                repeated_proof_here += 1
            seen_proof_signatures.add(signature)
        if repeated_proof_here:
            repeated_proof_boundaries += 1
            repeated_proof_calls += repeated_proof_here

        candidate_reasons: list[str] = []
        if (
            conf == ShadowConfidence.CERTAIN_MECHANICAL.value
            and would == ShadowWouldCall.NO.value
        ):
            candidate_reasons.append("prospective_runtime")
        if effect in {
            ShadowEffectKind.PROOF_ONLY.value,
            ShadowEffectKind.PROTOCOL_RECOVERY.value,
            ShadowEffectKind.REPORT_ONLY.value,
            ShadowEffectKind.NO_PROGRESS.value,
        }:
            candidate_reasons.append(effect)
        if repeated_read_here:
            candidate_reasons.append("repeated_unchanged_read")
        if repeated_proof_here:
            candidate_reasons.append("repeated_same_state_proof")
        if count("shadow_tool_error_count"):
            tool_error_boundaries += 1

        if candidate_reasons:
            candidate_boundaries.add(event_index)
            candidate_prompt_tokens += event_prompt
            candidate_completion_tokens += event_completion
            candidate_cache_read_tokens += event_cache
            for reason in dict.fromkeys(candidate_reasons):
                add_candidate_reason(reason)

        top_costly_boundaries.append(
            {
                "step": count("step"),
                "effect_kind": effect or "unknown",
                "would_call_model": would or "unknown",
                "reason_codes": str(event.get("shadow_reason_codes") or ""),
                "prompt_tokens": event_prompt,
                "cache_read_tokens": event_cache,
                "uncached_prompt_tokens": max(0, event_prompt - event_cache),
                "completion_tokens": event_completion,
                "tool_names": str(event.get("shadow_tool_names") or ""),
                "tool_error_count": count("shadow_tool_error_count"),
                "candidate_reasons": candidate_reasons,
            }
        )

    total = len(decision_events)
    top_costly_boundaries.sort(
        key=lambda item: (
            int(item["uncached_prompt_tokens"]) + int(item["completion_tokens"]),
            int(item["prompt_tokens"]) + int(item["completion_tokens"]),
        ),
        reverse=True,
    )
    return {
        "version": SHADOW_VERSION,
        "main_model_boundaries": total,
        "provider_failure_boundaries": len(failure_events),
        "would_call_model": decisions,
        "confidence": dict(sorted(confidence.items())),
        "effect_kinds": dict(sorted(effects.items())),
        "reason_codes": dict(sorted(reason_counts.items())),
        "certain_mechanical_boundaries": certain_mechanical,
        "certain_mechanical_fraction": round(certain_mechanical / total, 6) if total else 0.0,
        "unknown_fraction": (
            round(decisions[ShadowWouldCall.UNKNOWN.value] / total, 6) if total else 0.0
        ),
        "request_chars": request_chars,
        "dynamic_chars": dynamic_chars,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_tokens": cache_read_tokens,
        "uncached_prompt_tokens": max(0, prompt_tokens - cache_read_tokens),
        "cost_by_effect": dict(sorted(cost_by_effect.items())),
        "top_costly_boundaries": top_costly_boundaries[:5],
        "tool_error_boundaries": tool_error_boundaries,
        "repeated_unchanged_reads": {
            "boundaries": repeated_read_boundaries,
            "calls": repeated_read_calls,
        },
        "repeated_same_state_proofs": {
            "boundaries": repeated_proof_boundaries,
            "calls": repeated_proof_calls,
        },
        "deterministic_transition_candidates": {
            "boundaries": len(candidate_boundaries),
            "fraction": round(len(candidate_boundaries) / total, 6) if total else 0.0,
            "reason_counts": dict(sorted(candidate_reason_counts.items())),
            # These are observed costs attached to candidate boundaries, not a
            # counterfactual claim that all of them would disappear.
            "prompt_tokens": candidate_prompt_tokens,
            "cache_read_tokens": candidate_cache_read_tokens,
            "uncached_prompt_tokens": max(
                0, candidate_prompt_tokens - candidate_cache_read_tokens
            ),
            "completion_tokens": candidate_completion_tokens,
        },
    }


__all__ = [
    "SHADOW_VERSION",
    "ShadowAuthority",
    "ShadowConfidence",
    "ShadowDecision",
    "ShadowEffectKind",
    "ShadowReason",
    "ShadowSnapshot",
    "ShadowStepEffect",
    "ShadowWouldCall",
    "audit_history_delta",
    "classify_before_model",
    "request_char_counts",
    "summarize_shadow_usage",
]
