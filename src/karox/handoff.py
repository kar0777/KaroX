"""Structured session handoff for cross-model and cross-client transfer.

A session belongs to KaroX, not to a particular model or hosted client.  When a
task moves between providers or clients the receiving side needs a compact,
verifiable snapshot of the real project state -- not a verbatim chat history.
This module derives that snapshot from a ``SessionRecord`` while keeping every
secret and credential reference out of the output.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional

from .models import repository_fingerprint
from .security import redact
from .sessions import SessionRecord


HANDOFF_SCHEMA_VERSION = 1


def _redact_list(items: Any, limit: int = 50) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [dict(redact(item)) if isinstance(item, dict) else dict(redact({"value": item}))
            for item in items[:limit]]


def _plans_remaining(plan: Any) -> list[dict[str, Any]]:
    """Keep only steps that are not marked completed."""
    if not isinstance(plan, list):
        return []
    remaining: list[dict[str, Any]] = []
    for item in plan[:64]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status in {"done", "completed", "skipped"}:
            continue
        remaining.append(dict(redact(item)))
    return remaining


def _model_history(provider_history: Any) -> list[dict[str, Any]]:
    """Summarize provider/model entries without exposing credentials.

    Each entry keeps the model id, role, a one-line content preview, tool-call
    names, and route audit metadata.  Argument bodies and full assistant text
    are truncated so the handoff stays compact and never carries a secret that a
    remote tool call might have echoed back.
    """
    if not isinstance(provider_history, list):
        return []
    summary: list[dict[str, Any]] = []
    entries = [entry for entry in provider_history if isinstance(entry, dict)][-128:]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        model = entry.get("model")
        item: dict[str, Any] = {"role": role, "model": model}
        content = entry.get("content")
        if isinstance(content, str):
            item["content_preview"] = content[:200]
        tool_calls = entry.get("tool_calls")
        if isinstance(tool_calls, list):
            item["tool_calls"] = [
                {"name": tc.get("name"), "id": tc.get("call_id") or tc.get("id")}
                for tc in tool_calls
                if isinstance(tc, dict)
            ][:16]
        tool_result = entry.get("tool_result") or entry.get("result")
        if isinstance(tool_result, dict):
            item["tool_result"] = {
                "ok": tool_result.get("ok"),
                "command": entry.get("core_name") or tool_result.get("command"),
            }
        route = entry.get("route")
        if role == "provider_audit":
            route = {key: value for key, value in entry.items() if key != "role"}
        if isinstance(route, dict):
            item["route"] = dict(redact(route))
        usage = entry.get("usage")
        if isinstance(usage, dict):
            item["usage"] = dict(redact(usage))
        summary.append(item)
    return summary


def build_handoff(
    record: SessionRecord,
    *,
    repository: Optional[Any] = None,
) -> dict[str, Any]:
    """Return a secret-free, structured handoff snapshot for ``record``.

    The document mirrors the structured-handoff contract: goal, constraints,
    what was done, changed files, commands, check results, errors, remaining
    steps, current Git state, active processes, model history, usage, and
    evidence.  It never includes full chat history, credential references, or
    secret values.
    """
    fingerprint = record.repo_fingerprint
    if repository is not None:
        try:
            fingerprint = repository_fingerprint(repository)
        except OSError:
            pass

    document: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "session_id": record.session_id,
        "generated_at": time.time(),
        "goal": str(redact(record.task)),
        "constraints": {
            "repository": record.repository,
            "repo_fingerprint": fingerprint,
            "branch": record.branch or "",
            "access_profile": record.access_profile,
            "skills": [
                {"name": item.get("name"), "version": item.get("version")}
                for item in record.skills
                if isinstance(item, dict)
            ],
            "mcp_servers": [
                {"server_id": item.get("server_id"), "namespace": item.get("namespace")}
                for item in record.mcp_servers
                if isinstance(item, dict)
            ],
        },
        "summary": str(redact(record.summary))[:4000],
        "done": {
            "summary": str(redact(record.summary))[:4000],
            "decisions": _redact_list(record.decisions),
            "checkpoints": _redact_list(record.checkpoints),
        },
        "changed_files": list(record.changed_files)[-200:],
        "commands": _redact_list(record.checks),
        "check_results": [
            {
                "correlation_id": item.get("correlation_id"),
                "ok": item.get("ok"),
                "exit_code": item.get("exit_code"),
                "timed_out": item.get("timed_out"),
                "argv": item.get("argv"),
            }
            for item in record.checks
            if isinstance(item, dict)
        ][-50:],
        "errors": _redact_list(record.failures),
        "remaining_steps": _plans_remaining(record.plan),
        "git_state": dict(redact(record.git_state)) if record.git_state else {},
        "active_processes": _redact_list(record.jobs),
        "model_history": _model_history(record.provider_history),
        "usage": dict(redact(record.usage)) if record.usage else {},
        "unfinished_actions": _redact_list(record.unfinished_actions),
        "evidence": [
            {
                "kind": item.get("kind"),
                "summary": item.get("summary"),
                "artifact_sha256": item.get("artifact_sha256"),
                "evidence_id": item.get("evidence_id"),
            }
            for item in record.evidence
            if isinstance(item, dict)
        ][-200:],
    }
    return _validated_handoff(document)


def _validated_handoff(document: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure the document is strict JSON and free of obvious secret markers.

    The content digest intentionally excludes volatile fields (``generated_at``
    and the digest itself) so two snapshots of identical state compare equal.
    """
    volatile = {"document_sha256", "generated_at"}
    stable = {key: value for key, value in document.items() if key not in volatile}
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    json.loads(encoded)  # round-trip validates strict JSON
    if "Bearer " in encoded or "os-keyring:" in encoded:
        raise ValueError("handoff document must not contain credentials or references")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    result = dict(document)
    result["document_sha256"] = digest
    return result


def handoff_digest(document: Mapping[str, Any]) -> str:
    """Return the content digest recorded on the handoff document."""
    value = document.get("document_sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("handoff document is missing its content digest")
    return value
