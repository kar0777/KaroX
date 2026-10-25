"""Proof-state readers for a persisted session record.

Seven small pure functions the engine kernel uses to decide what a session
already proved: which writes happened, what the evidence chain selected, what
checks the completion floor still owes, and what the model's answer rests on.
They read only the ``SessionRecord`` the session store checksums, so their
output is as trustworthy as the record itself. Extracted verbatim from the
retired v5 loop so the engine inherits its exact semantics.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from . import completion_gate
from .agent import (
    HARNESS_REQUESTED_BY,
    INSPECTION_TOOLS,
    MUTATION_TOOLS,
    _strict_json_loads,
)
from .security import redact
from .sessions import SessionRecord


def safe_arguments(raw: str) -> tuple[str, bool]:
    """Redacted argument text plus whether redaction changed nothing."""
    try:
        value = _strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        safe = str(redact(raw))
        return safe, safe == raw
    safe_value = redact(value)
    return (
        json.dumps(safe_value, ensure_ascii=False, sort_keys=True),
        safe_value == value,
    )


def step_count(history: List[Dict[str, Any]]) -> int:
    """Assistant turns that carried a provider response."""
    return sum(
        1
        for entry in history
        if entry.get("role") == "assistant" and entry.get("provider")
    )


def unresolved_model_checks(
    record: SessionRecord, missing: Sequence[str]
) -> list[list[str]]:
    """Exact argv of approved checks the floor still owes a rerun, oldest first."""

    owed = {
        reason[len("unresolved failed check: "):]
        for reason in missing
        if reason.startswith("unresolved failed check: ")
    }
    found: list[list[str]] = []
    for entry in record.provider_history:
        if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
            continue
        result = entry.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or data.get("verification_eligible") is not True:
            continue
        argv = data.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            continue
        if " ".join(argv) in owed and argv not in found:
            found.append(list(argv))
    return found


def evidence_chain(record: SessionRecord) -> bool:
    """Whether the record holds a complete write -> check -> status/diff chain.

    Who asked for a call is recorded, and only the two known authors are
    evidence: the model's own calls, and the harness's finishing checks. An
    unknown author is treated the way an unverified write is, because the
    chain exists to prove what happened, not to collect anything that claims
    it did.
    """
    write: Optional[Dict[str, Any]] = None
    check: Optional[Dict[str, Any]] = None
    status: Optional[Dict[str, Any]] = None
    diff: Optional[Dict[str, Any]] = None
    for entry in record.provider_history:
        if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
            continue
        result = entry["result"]
        if result.get("requested_by") not in (None, HARNESS_REQUESTED_BY):
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        core_name = entry.get("core_name")
        ok = result.get("ok") is True
        if core_name in MUTATION_TOOLS:
            if ok and data.get("changed") is True:
                write, check, status, diff = result, None, None, None
            elif not ok:
                write, check, status, diff = None, None, None, None
        elif core_name == "checks.run" and write is not None:
            if ok and data.get("verification_eligible") is True:
                check, status, diff = result, None, None
            else:
                check, status, diff = None, None, None
        elif core_name == "git.status" and check is not None:
            status = result if ok else None
        elif core_name == "git.diff" and check is not None:
            diff = result if ok else None
    if completion_gate.docs_only(record):
        # No behavioral obligation for text; keep persisted change/review evidence.
        status = diff = None
        for entry in record.provider_history:
            result = entry.get("result") or {}
            if entry.get("core_name") in MUTATION_TOOLS:
                status = diff = None
            elif result.get("ok") and result.get("requested_by") in (None, HARNESS_REQUESTED_BY):
                if entry.get("core_name") == "git.status":
                    status = result
                elif entry.get("core_name") == "git.diff":
                    diff = result
        selected = (write, status, diff)
    else:
        selected = (write, check, status, diff)
    if any(item is None for item in selected):
        return False
    persisted_ids = {
        item.get("evidence_id")
        for item in record.evidence
        if isinstance(item, dict)
    }
    for result in selected:
        assert result is not None
        evidence = result.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
        if any(item.get("evidence_id") not in persisted_ids for item in evidence):
            return False
    return True


def attempted_mutation(record: SessionRecord) -> bool:
    """Whether any mutation tool call appears in the record."""
    return any(
        entry.get("role") == "tool" and entry.get("core_name") in MUTATION_TOOLS
        for entry in record.provider_history
    )


def answer_basis(record: SessionRecord) -> tuple[Dict[str, Any], ...]:
    """The successful inspections an answer rests on.

    Reads record no Core evidence of their own, so the basis is drawn from
    the persisted tool history, which the session store checksums as one
    document. It is reported rather than merely counted: a verified answer
    that named no source would be exactly the narrative-as-proof this
    product exists to refuse.
    """
    basis: List[Dict[str, Any]] = []
    for entry in record.provider_history:
        if entry.get("role") != "tool":
            continue
        core_name = entry.get("core_name")
        if core_name not in INSPECTION_TOOLS:
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        item: Dict[str, Any] = {
            "tool": core_name,
            "call_id": str(entry.get("tool_call_id", "")),
        }
        for key in ("path", "sha256", "stdout_sha256"):
            value = data.get(key)
            if isinstance(value, str) and value:
                item[key] = value
        basis.append(item)
    return tuple(basis)


def last_provider_message(record: SessionRecord) -> Optional[str]:
    """The model's latest surviving answer text, redacted."""
    for entry in reversed(record.provider_history):
        if entry.get("role") == "assistant" and entry.get("content") is not None:
            return str(redact(entry["content"]))
    return None
