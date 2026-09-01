"""Durable ChatGPT Project binding for the hosted KaroX runtime.

ChatGPT's MCP request does not currently expose a trusted ChatGPT Project ID to
third-party MCP servers.  KaroX therefore uses an explicit, non-secret project
capsule placed in the ChatGPT Project instructions.  The capsule provides
stable accidental-scope isolation and a durable resume key without pretending
to be an authentication credential; OAuth/session policy remains the security
boundary.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .security import redact
from .sessions import SessionError, SessionStore
from .task_state import TaskStateStore

_SCHEMA_VERSION = 1
_FILENAME = "chatgpt_project_binding.json"
_MAX_NAME = 120
_MAX_COMPACT_BYTES = 24 * 1024


class ChatGPTProjectBindingError(RuntimeError):
    """Invalid or mismatched ChatGPT Project binding."""


def _clean_name(value: str) -> str:
    if not isinstance(value, str):
        raise ChatGPTProjectBindingError("project name must be text")
    cleaned = " ".join(str(redact(value)).split()).strip()
    if not cleaned:
        raise ChatGPTProjectBindingError("project name must not be empty")
    if len(cleaned) > _MAX_NAME:
        raise ChatGPTProjectBindingError(f"project name must be {_MAX_NAME} characters or fewer")
    return cleaned


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _bounded(value: Any, limit: int = 1400) -> Any:
    safe = redact(value)
    if isinstance(safe, str):
        return safe if len(safe) <= limit else safe[: limit - 1] + "…"
    if isinstance(safe, (int, float, bool)) or safe is None:
        return safe
    if isinstance(safe, list):
        return [_bounded(item, max(120, limit // 4)) for item in safe[-20:]]
    if isinstance(safe, tuple):
        return [_bounded(item, max(120, limit // 4)) for item in safe[-20:]]
    if isinstance(safe, dict):
        result: dict[str, Any] = {}
        for key in list(safe)[-30:]:
            result[str(key)[:120]] = _bounded(safe[key], max(120, limit // 4))
        return result
    return _bounded(str(safe), limit)


@dataclass(frozen=True)
class ChatGPTProjectBinding:
    project_name: str
    binding_id: str
    session_id: str
    created_at: float
    updated_at: float

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChatGPTProjectBinding":
        if int(payload.get("schema_version", 0)) != _SCHEMA_VERSION:
            raise ChatGPTProjectBindingError("unsupported ChatGPT Project binding schema")
        project_name = _clean_name(payload.get("project_name", ""))
        binding_id = payload.get("binding_id")
        session_id = payload.get("session_id")
        if not isinstance(binding_id, str) or not 20 <= len(binding_id) <= 96:
            raise ChatGPTProjectBindingError("ChatGPT Project binding id is invalid")
        if not isinstance(session_id, str) or not session_id:
            raise ChatGPTProjectBindingError("ChatGPT Project binding session is invalid")
        return cls(
            project_name=project_name,
            binding_id=binding_id,
            session_id=session_id,
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "project_name": self.project_name,
            "binding_id": self.binding_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def instruction_capsule(self) -> str:
        return (
            "KaroX ChatGPT Project binding\n"
            f"Project: {self.project_name}\n"
            f"Binding: {self.binding_id}\n"
            "When using KaroX in this ChatGPT Project, call "
            "karox.chatgpt_project.resume with this binding before substantial work "
            "or after a new chat/reconnect. Treat the binding as a scope marker, not a secret."
        )


class ChatGPTProjectBindingStore:
    """Persist one ChatGPT Project capsule beside a durable KaroX session."""

    def __init__(self, sessions: SessionStore, session_id: str) -> None:
        self.sessions = sessions
        self.session_id = session_id
        self.task_states = TaskStateStore(sessions)

    @property
    def path(self) -> Path:
        return self.sessions.session_dir(self.session_id) / _FILENAME

    def load_optional(self) -> Optional[ChatGPTProjectBinding]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ChatGPTProjectBindingError("ChatGPT Project binding is unreadable") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChatGPTProjectBindingError("ChatGPT Project binding is corrupt") from exc
        if not isinstance(payload, dict):
            raise ChatGPTProjectBindingError("ChatGPT Project binding is corrupt")
        binding = ChatGPTProjectBinding.from_dict(payload)
        if binding.session_id != self.session_id:
            raise ChatGPTProjectBindingError("ChatGPT Project binding belongs to another session")
        return binding

    def bind(self, project_name: str, *, rotate: bool = False) -> ChatGPTProjectBinding:
        name = _clean_name(project_name)
        session = self.sessions.load(self.session_id)
        if session.revoked:
            raise ChatGPTProjectBindingError("KaroX session has been revoked")
        existing = self.load_optional()
        if existing is not None and not rotate:
            if existing.project_name != name:
                raise ChatGPTProjectBindingError(
                    "this KaroX session is already bound to another ChatGPT Project; rotate explicitly"
                )
            return existing
        now = time.time()
        binding = ChatGPTProjectBinding(
            project_name=name,
            binding_id="kxp_" + secrets.token_urlsafe(24),
            session_id=self.session_id,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        _atomic_json(self.path, binding.to_dict())
        return binding

    def require(self, binding_id: str) -> ChatGPTProjectBinding:
        if not isinstance(binding_id, str) or not binding_id:
            raise ChatGPTProjectBindingError("binding is required")
        binding = self.load_optional()
        if binding is None:
            raise ChatGPTProjectBindingError(
                "this KaroX session is not bound to a ChatGPT Project; call karox.chatgpt_project.bind first"
            )
        if not secrets.compare_digest(binding.binding_id, binding_id):
            raise ChatGPTProjectBindingError(
                "ChatGPT Project binding mismatch; use KaroX from the bound project instructions"
            )
        return binding

    def compact_snapshot(self, binding_id: str) -> dict[str, Any]:
        binding = self.require(binding_id)
        try:
            session = self.sessions.load(self.session_id)
        except SessionError as exc:
            raise ChatGPTProjectBindingError(str(exc)) from exc

        workstream_ids = self.task_states.list_workstreams(self.session_id)
        states: list[dict[str, Any]] = []
        default = self.task_states.load_optional(self.session_id)
        if default is not None:
            states.append(self._compact_task_state("default", default))
        for workstream_id in workstream_ids[:16]:
            state = self.task_states.load_optional(
                self.session_id, workstream_id=workstream_id
            )
            if state is not None:
                states.append(self._compact_task_state(workstream_id, state))
        total_workstreams = len(workstream_ids) + (1 if default is not None else 0)

        payload: dict[str, Any] = {
            "ok": True,
            "schema_version": 1,
            "chatgpt_project": {
                "name": binding.project_name,
                "binding": binding.binding_id,
                "binding_is_authentication": False,
            },
            "session": {
                "session_id": session.session_id,
                "status": session.status,
                "phase": session.phase,
                "task": _bounded(session.task, 1800),
                "summary": _bounded(session.summary, 3500),
                "plan": _bounded(session.plan, 3500),
                "decisions": _bounded(session.decisions, 2500),
                "changed_files": list(session.changed_files[-80:]),
                "checks": _bounded(session.checks, 3000),
                "failures": _bounded(session.failures, 2500),
                "unfinished_actions": _bounded(session.unfinished_actions, 2500),
                "revision": session.revision,
                "updated_at": session.updated_at,
            },
            "workstreams": states,
            "continuation": {
                "instruction": (
                    "Continue from this compact KaroX state. Read an artifact or inspect the repository "
                    "only when the compact evidence is insufficient; do not replay already completed work."
                ),
                "workstream_count": total_workstreams,
                "workstreams_returned": len(states),
                "workstreams_truncated": len(states) < total_workstreams,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > _MAX_COMPACT_BYTES:
            payload["session"]["checks"] = _bounded(session.checks, 900)
            payload["session"]["failures"] = _bounded(session.failures, 900)
            payload["session"]["plan"] = _bounded(session.plan, 1500)
            payload["session"]["decisions"] = _bounded(session.decisions, 1000)
            payload["workstreams"] = states[:8]
            payload["continuation"]["workstreams_returned"] = len(payload["workstreams"])
            payload["continuation"]["workstreams_truncated"] = (
                len(payload["workstreams"]) < total_workstreams
            )
            payload["continuation"]["truncated_for_transport"] = True

        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > _MAX_COMPACT_BYTES:
            # Hard transport fallback. Preserve the durable identity and enough
            # continuation truth to choose the next read, but never make a new
            # ChatGPT chat ingest an unbounded session/workstream payload.
            payload["session"] = {
                "session_id": session.session_id,
                "status": session.status,
                "phase": session.phase,
                "task": _bounded(session.task, 600),
                "summary": _bounded(session.summary, 800),
                "plan": _bounded(session.plan, 800),
                "decisions": _bounded(session.decisions, 500),
                "changed_files": list(session.changed_files[-20:]),
                "checks": _bounded(session.checks, 500),
                "failures": _bounded(session.failures, 500),
                "unfinished_actions": _bounded(session.unfinished_actions, 500),
                "revision": session.revision,
                "updated_at": session.updated_at,
            }
            payload["workstreams"] = []
            payload["continuation"]["workstreams_returned"] = 0
            payload["continuation"]["workstreams_truncated"] = total_workstreams > 0
            payload["continuation"]["truncated_for_transport"] = True
            payload["continuation"]["transport_minimal"] = True
        return payload

    @staticmethod
    def _compact_task_state(workstream_id: str, state: Any) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for key in (
            "objective",
            "project_id",
            "phase",
            "status",
            "summary",
            "next_action",
            "last_checkpoint_timestamp",
            "baseline_artifact_id",
        ):
            item = state.facts.get(key)
            if item is None:
                continue
            facts[key] = {
                "value": _bounded(item.value, 1200),
                "origin": item.origin.value,
            }
        return {
            "workstream_id": workstream_id,
            "task_id": state.task_id,
            "revision": state.revision,
            "updated_at": state.updated_at,
            "facts": facts,
        }
