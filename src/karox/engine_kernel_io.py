"""Engine kernel durable-IO mixin: transcript seeding, persistence and snapshots."""

from __future__ import annotations

import logging
import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

logger = logging.getLogger("karox.engine_kernel")

import hashlib
import json
import pathlib
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from .engine_provider_bridge import (  # noqa: E402
    _response_from_engine_message,
)
from . import session_analysis
from .state_briefing import (  # noqa: E402
    build_state_briefing,
)
from .agent import (  # noqa: E402
    HARNESS_ORIGIN,
)
from .session_analysis import HARNESS_REQUESTED_BY
from .models import CoreResult
from .providers import (  # noqa: E402
    ModelResponse,
)
from .security import redact, redact_content
from .sessions import MutationLease, SessionRecord
from .usage_analytics import merge_response_usage, usage_event_from_response


_MAX_SNAPSHOT_FILES = 50
_MAX_SNAPSHOT_BYTES = 2_000_000


class EngineIoMixin:
    """Methods composed into EngineKernel; see engine_kernel.py."""

    def _seed_history(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> Tuple[Optional[List[Dict[str, Any]]], int]:
        """Initialize or reconstruct the engine transcript from the session."""
        if not record.provider_history:

            def update(current: SessionRecord) -> None:
                current.status = "active"
                current.phase = "execution"

            self._update_session(session_id, lease, update)
            return None, 0
        messages: List[Dict[str, Any]] = []
        assistant_steps = 0
        for entry in record.provider_history:
            role = entry.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            message: Dict[str, Any] = {
                "role": str(role),
                "content": entry.get("content"),
            }
            if role == "assistant":
                raw_calls = entry.get("tool_calls")
                if isinstance(raw_calls, list) and raw_calls:
                    message["tool_calls"] = [
                        {
                            "call_id": str(item.get("call_id", "")),
                            "name": str(item.get("name", "")),
                            "raw_arguments": str(item.get("raw_arguments", "")),
                        }
                        for item in raw_calls
                        if isinstance(item, Mapping)
                    ]
                blocks = entry.get("reasoning_blocks")
                if isinstance(blocks, list) and blocks:
                    message["reasoning_blocks"] = blocks
                assistant_steps += 1
            if role == "tool":
                call_id = str(entry.get("tool_call_id", ""))
                if not call_id:
                    continue
                message["tool_call_id"] = call_id
                self._persisted_call_ids.add(call_id)
            messages.append(message)
        if not self._task_covered(record):
            current_task = str(record.task)
            self._append_message(
                session_id,
                lease,
                {
                    "role": "user",
                    "content": current_task,
                    "kind": "task",
                    "origin": self.origin.key,
                },
                phase="execution",
            )
            # Insert before the first assistant, not at the end: a user message
            # between an assistant tool call and its result is rejected by
            # transports that pair them (OpenAI-compatible, Anthropic).
            insert_at = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if message.get("role") == "assistant"
                ),
                len(messages),
            )
            messages.insert(insert_at, {"role": "user", "content": current_task})
        if not messages:
            return None, 0
        answered: Set[str] = {
            str(item.get("tool_call_id", ""))
            for item in messages
            if item.get("role") == "tool"
        }
        # A run interrupted between the model's call and its execution leaves
        # a tool_use without a tool_result; a transport that pairs them would
        # refuse the whole resumed request. Insert the synthetic result
        # immediately after its assistant message: appending all of them at
        # the end left assistant/user/tool messages in between. Synthesized in
        # memory only; the durable record still says the call never ran.
        rebuilt: List[Dict[str, Any]] = []
        for message in messages:
            rebuilt.append(message)
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                call_id = str(call.get("call_id", ""))
                if not call_id or call_id in answered:
                    continue
                answered.add(call_id)
                rebuilt.append(
                    {
                        "role": "tool",
                        "content": (
                            "<returncode>-1</returncode>\n<output>\nthe run "
                            "was interrupted before this call executed; it did "
                            "not run and changed nothing\n</output>"
                        ),
                        "tool_call_id": call_id,
                    }
                )
        return rebuilt, assistant_steps

    @staticmethod
    def _task_covered(record: SessionRecord) -> bool:
        task = str(record.task)
        if not task:
            return True
        for entry in record.provider_history:
            if entry.get("kind") == "task" and str(entry.get("content", "")).endswith(task):
                return True
        # The first user turn of an engine run is the rendered instance
        # template, which ends with the task. Requiring exact equality made
        # every engine-written session look uncovered and injected a second
        # task message at the end of a resumed transcript.
        return any(
            entry.get("role") == "user"
            and str(entry.get("content", "")).endswith(task)
            for entry in record.provider_history
        )

    def _on_engine_messages(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Persist new engine messages as durable session checkpoints.

        Tool results carry their own persistence path (the environment writes
        the full CoreResult); here only messages the environment did not
        already persist are written, deduplicated by tool call id.
        """
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        for message in messages:
            role = message.get("role")
            extra = message.get("extra") or {}
            if role == "exit":
                # Only a real submission is an answer: the engine's limit and
                # interruption exits carry their status as content, not prose.
                if str(extra.get("exit_status") or "") == "Submitted":
                    submission = str(extra.get("submission") or message.get("content") or "")
                    if submission:
                        self._engine_submission = submission
                        if str(extra.get("exit_reason") or "") == "prose-final":
                            # The prose answer was never added as an assistant
                            # message (the bridge raised FormatError for it),
                            # so the durable transcript gets it here.
                            self._append_message(
                                self._session_id,
                                self._lease,
                                {
                                    "role": "assistant",
                                    "origin": self.origin.key,
                                    "content": str(redact(submission)),
                                    "tool_calls": [],
                                    "provider": self.provider.provider_name,
                                    "model": self.model,
                                    "finish_reason": "stop",
                                    "usage": {},
                                },
                            )
                continue
            if role == "assistant":
                self._persist_engine_assistant(session_id, lease, message)
                continue
            if role == "tool":
                call_id = str(message.get("tool_call_id", ""))
                if not call_id or call_id in self._persisted_call_ids:
                    continue
                self._persisted_call_ids.add(call_id)
                self._append_message(
                    session_id,
                    lease,
                    {
                        "role": "tool",
                        "origin": self.origin.key,
                        "content": str(message.get("content") or ""),
                        "tool_call_id": call_id,
                    },
                )
                continue
            content = message.get("content")
            if self._format_error_response(message) is not None:
                self._persist_format_error_nudge(session_id, lease, message)
                continue
            history = self.sessions.load(session_id).provider_history
            # Keep the caller's kind: a repair prompt is engine_repair, not an
            # anonymous engine_note. Only a message without one gets the
            # first-turn/observation default.
            kind = str(message.get("kind") or "")
            if not kind:
                kind = "task" if not history and role == "user" else "engine_note"
            self._append_message(
                session_id,
                lease,
                {
                    "role": str(role),
                    "content": str(content) if content is not None else "",
                    "kind": kind,
                    "origin": self.origin.key,
                },
                phase="execution",
            )

    def _format_error_response(self, message: Mapping[str, Any]) -> Optional[ModelResponse]:
        """Rebuild the billed response behind a format-error nudge, if any.

        The nudge's ``extra`` carries the wire facts the bridge persisted when
        the model's answer failed the action contract. Tokens for that answer
        were spent, so the session ledger must count them exactly as the
        legacy loop counted a response whose tool calls failed validation.
        """
        extra = message.get("extra") or {}
        if not isinstance(extra, Mapping) or str(extra.get("interrupt_type") or "") != "FormatError":
            return None
        usage = extra.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None
        response = extra.get("response")
        choices = response.get("choices") if isinstance(response, Mapping) else None
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
        wire = choice.get("message") if isinstance(choice, Mapping) else None
        content = wire.get("content") if isinstance(wire, Mapping) else None
        return ModelResponse(
            content=content if isinstance(content, str) else None,
            tool_calls=(),
            finish_reason=choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None,
            usage={str(key): value for key, value in usage.items()},
            transport_attempts=1,
            selected_provider=extra.get("provider") if isinstance(extra.get("provider"), str) else None,
            selected_model=extra.get("model") if isinstance(extra.get("model"), str) else None,
        )

    def _persist_format_error_nudge(
        self, session_id: str, lease: MutationLease, message: Mapping[str, Any]
    ) -> None:
        response = self._format_error_response(message)
        if response is None:
            return
        content = message.get("content")
        entry = {
            "role": "user",
            "content": str(content) if content is not None else "",
            "kind": "engine_note",
            "origin": self.origin.key,
        }
        usage_event = usage_event_from_response(
            session_id=session_id,
            step=max(1, self._step),
            response=response,
        )
        usage_event["kind"] = "format_error"

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )

        self._update_session(session_id, lease, update)

    def _record_side_usage(self, response: ModelResponse, kind: str = "visual_check") -> None:
        """Merge a request made outside the main loop into the session usage."""
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        event = usage_event_from_response(
            session_id=session_id, step=max(1, self._step), response=response
        )
        event["kind"] = kind

        def update(record: SessionRecord) -> None:
            record.usage = merge_response_usage(
                record.usage, response, event=event, model_fallback=self.model
            )
            side = record.usage.get("side_requests")
            counts = dict(side) if isinstance(side, dict) else {}
            counts[kind] = int(counts.get(kind) or 0) + 1
            record.usage["side_requests"] = counts

        self._update_session(session_id, lease, update)

    def _update_session(
        self,
        session_id: str,
        lease: MutationLease,
        update: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        record = self.sessions.load(session_id)
        update(record)
        saved = self.sessions.save(record, record.revision, lease)
        self._known_revision = saved.revision
        return saved

    def _append_message(
        self,
        session_id: str,
        lease: MutationLease,
        entry: Dict[str, Any],
        *,
        phase: Optional[str] = None,
    ) -> None:
        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            if phase:
                record.phase = phase

        self._update_session(session_id, lease, update)

    def _heartbeat(self) -> None:
        lease = self._lease
        if lease is None:
            return
        try:
            self.sessions.heartbeat(
                lease,
                ttl_seconds=min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0)),
                session_revision=self._known_revision,
            )
        except Exception:
            pass

    def _persist_engine_tool_result(
        self,
        *,
        call_id: str,
        alias: str,
        core_name: str,
        result: CoreResult,
        output: Mapping[str, Any],
        requested_by: str,
    ) -> None:
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        if call_id:
            self._persisted_call_ids.add(call_id)
        harness_call = requested_by == HARNESS_REQUESTED_BY
        if harness_call:
            for item in result.evidence:
                item.metadata["requested_by"] = HARNESS_REQUESTED_BY
        result_value = result.to_dict()
        if harness_call:
            result_value["requested_by"] = HARNESS_REQUESTED_BY
        content = str(output.get("observation") or output.get("output") or "")
        entry: Dict[str, Any]
        if call_id:
            entry = {
                "role": "tool",
                "origin": HARNESS_ORIGIN if harness_call else self.origin.key,
                "content": redact_content(content),
                "tool_call_id": call_id,
                "tool_name": alias,
                "core_name": core_name,
                "result": redact_content(result_value),
            }
        else:
            # A fenced command without a tool call id: the engine itself feeds
            # this back as a user observation, and the durable record matches.
            entry = {
                "role": "user",
                "origin": self.origin.key,
                "content": redact_content(content),
                "kind": "engine_observation",
            }

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            stamped = {item.evidence_id: item.to_dict() for item in result.evidence}
            record.evidence = [
                stamped.get(item.get("evidence_id"), item) for item in record.evidence
            ]
            if not result.mutation:
                known = {
                    item.get("evidence_id")
                    for item in record.evidence
                    if isinstance(item, dict)
                }
                for item in result.evidence:
                    if item.evidence_id not in known:
                        record.evidence.append(item.to_dict())
            if core_name in {"git.status", "git.diff"}:
                key = "status" if core_name == "git.status" else "diff"
                data = result.data if isinstance(result.data, dict) else {}
                record.git_state[key] = {
                    "correlation_id": result.correlation_id,
                    "ok": result.ok,
                    "exit_code": data.get("exit_code"),
                    "timed_out": data.get("timed_out", False),
                    "sha256": data.get("sha256"),
                    "stdout": data.get("stdout", ""),
                }

        self._update_session(session_id, lease, update)

    def _persist_engine_tool_error(
        self,
        call_id: str,
        alias: str,
        core_name: Optional[str],
        error_type: str,
        message: str,
    ) -> None:
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        payload = {
            "ok": False,
            "error": {"type": error_type, "message": str(redact(message))[:4000]},
        }
        entry: Dict[str, Any]
        if call_id:
            entry = {
                "role": "tool",
                "origin": self.origin.key,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "tool_call_id": call_id,
                "tool_name": alias,
                "core_name": core_name,
                "error": payload["error"],
            }
        else:
            entry = {
                "role": "user",
                "origin": self.origin.key,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "kind": "engine_observation",
            }
        self._append_message(session_id, lease, entry)

    def _record_step_usage(self, event: Mapping[str, Any]) -> None:
        for key, value in event.items():
            if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool):
                self._last_step_usage[key] = self._last_step_usage.get(key, 0) + max(0, value)

    def _persist_engine_assistant(
        self, session_id: str, lease: MutationLease, message: Mapping[str, Any]
    ) -> None:
        response = _response_from_engine_message(message)
        tool_calls = []
        for item in response.tool_calls:
            safe_arguments, recoverable = session_analysis.safe_arguments(
                item.raw_arguments
            )
            tool_calls.append(
                {
                    "call_id": item.call_id,
                    "name": item.name,
                    "raw_arguments": safe_arguments,
                    "arguments_sha256": hashlib.sha256(
                        item.raw_arguments.encode("utf-8")
                    ).hexdigest(),
                    "recoverable": recoverable,
                }
            )
        entry: Dict[str, Any] = {
            "role": "assistant",
            "origin": self.origin.key,
            "content": redact(response.content) if response.content is not None else None,
            "reasoning_summary": (
                redact(response.reasoning_summary)
                if response.reasoning_summary is not None
                else None
            ),
            "tool_calls": tool_calls,
            "provider": response.selected_provider or self.provider.provider_name,
            "model": response.selected_model or self.model,
            "finish_reason": response.finish_reason,
            "response_id": response.response_id,
            "transport_attempts": response.transport_attempts,
            "usage": dict(response.usage),
        }
        telemetry = (message.get("extra") or {}).get("prompt_cache")
        if isinstance(telemetry, Mapping):
            # Digest-derived counts only: why this request did or did not hit
            # the provider's prefix cache.
            entry["prompt_cache"] = dict(telemetry)
        stored = dict(redact(entry))
        blocks = message.get("reasoning_blocks")
        if isinstance(blocks, list) and blocks:
            # Transport form, never display-redacted: a signed block that no
            # longer matches its signature is rejected by the provider.
            stored["reasoning_blocks"] = blocks
        usage_event = usage_event_from_response(
            session_id=session_id,
            step=max(1, self._step),
            response=response,
        )

        def update(record: SessionRecord) -> None:
            record.provider_history.append(stored)
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )
            if isinstance(telemetry, Mapping) and telemetry.get("full_miss") is True:
                misses = record.usage.get("prompt_cache_misses")
                counts = dict(misses) if isinstance(misses, dict) else {}
                cause = str(telemetry.get("cause") or "unknown")
                counts[cause] = int(counts.get(cause) or 0) + 1
                record.usage["prompt_cache_misses"] = counts

        self._update_session(session_id, lease, update)
        self._record_step_usage(usage_event)

    def state_briefing(self) -> str:
        """The deterministic session snapshot the agent appends after compaction.

        Built from the record, never from a model summary: requirements, the
        model's own plan, changed files, the latest approved check and its
        parsed failures all survive a compaction that drops the messages that
        first carried them.
        """

        if self._session_id is None:
            return ""
        try:
            record = self.sessions.load(self._session_id)
        except Exception:  # noqa: BLE001 - a missing snapshot must not break a turn
            return ""
        return build_state_briefing(
            task=record.task,
            requirements=record.requirements,
            plan_items=record.plan_items,
            changed_files=record.changed_files,
            history=record.provider_history,
        )

    def _snapshot_verified_state(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> None:
        if not self._restore_on_regression or not record.changed_files:
            return
        files = list(record.changed_files)[:_MAX_SNAPSHOT_FILES]
        directory = self.sessions.session_dir(session_id) / "verified"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        digests: Dict[str, str] = {}
        for relative in files:
            try:
                source = self.core.safe_path(relative)
            except Exception:
                continue
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if len(payload) > _MAX_SNAPSHOT_BYTES:
                continue
            target = directory / pathlib.PurePosixPath(str(relative))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            except OSError:
                continue
            digests[str(relative)] = hashlib.sha256(payload).hexdigest()
        if not digests:
            return
        snapshot = {
            "step": session_analysis.step_count(record.provider_history),
            "files": digests,
            "checked_at": time.time(),
        }

        def update(current: SessionRecord) -> None:
            current.verified_snapshot = dict(snapshot)

        try:
            self._update_session(session_id, lease, update)
        except Exception:
            # A failed snapshot write must not fail a run that succeeded; the
            # record still says verified.
            return

    def _restore_verified_state(
        self, session_id: str, record: SessionRecord
    ) -> Optional[str]:
        if not self._restore_on_regression:
            return None
        stored = dict(record.verified_snapshot) if record.verified_snapshot else {}
        files = stored.get("files")
        if not isinstance(files, dict) or not files:
            return None
        directory = self.sessions.session_dir(session_id) / "verified"
        restored: List[str] = []
        for relative, digest in files.items():
            source = directory / pathlib.PurePosixPath(str(relative))
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(payload).hexdigest() != str(digest):
                continue
            try:
                target = self.core.safe_path(str(relative))
            except Exception:
                continue
            try:
                if target.is_file() and target.read_bytes() == payload:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                restored.append(str(relative))
            except OSError:
                continue
        if not restored:
            return None
        return (
            f"restored the verified state from step {stored.get('step', '?')} "
            f"({', '.join(restored[:5])})"
        )
