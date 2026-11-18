"""Session persistence: one append-only JSONL file per session, enough to resume.

Records: ``meta`` (once), ``message``, ``usage``, ``reset`` (full message list after a
compaction), ``event`` (run_finished). Loading replays them in order.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.core.messages import Message, Usage

_SESSION_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")


def default_home() -> Path:
    env = os.environ.get("KAROX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".karox6"


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def valid_session_id(session_id: str) -> bool:
    """A plain directory name: an id from a client can never point outside the store."""
    return bool(_SESSION_ID.fullmatch(session_id)) and ".." not in session_id


@dataclass(slots=True)
class LoadedSession:
    session_id: str
    meta: dict[str, Any]
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0


class SessionStore:
    def __init__(self, home: Path, session_id: str) -> None:
        self.home = home
        self.session_id = session_id
        self.dir = home / "sessions" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "session.jsonl"

    def append(self, kind: str, **data: Any) -> None:
        record = {"kind": kind, "ts": round(time.time(), 3), **data}
        line = json.dumps(record, ensure_ascii=False, default=str)
        # errors="replace": a stray surrogate in a tool result must not end the session.
        with self.path.open("a", encoding="utf-8", errors="replace", newline="\n") as fh:
            fh.write(line + "\n")

    def message(self, message: Message) -> None:
        self.append("message", message=message.to_dict())

    def reset(self, messages: list[Message]) -> None:
        self.append("reset", messages=[m.to_dict() for m in messages])

    @staticmethod
    def load(home: Path, session_id: str) -> LoadedSession:
        path = home / "sessions" / session_id / "session.jsonl"
        if not valid_session_id(session_id) or not path.is_file():
            raise FileNotFoundError(f"no session {session_id!r} under {home / 'sessions'}")
        loaded = LoadedSession(session_id=session_id, meta={})
        # Split on "\n" only: splitlines() also breaks on U+2028 and other separators
        # that json.dumps(ensure_ascii=False) leaves inside strings.
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line after a crash
            kind = record.get("kind")
            if kind == "meta":
                loaded.meta = record
            elif kind == "message":
                loaded.messages.append(Message.from_dict(record["message"]))
            elif kind == "reset":
                loaded.messages = [Message.from_dict(m) for m in record["messages"]]
            elif kind == "usage":
                loaded.usage.add(Usage.from_dict(record.get("usage") or {}))
                loaded.cost_usd += float(record.get("cost_usd") or 0.0)
        # A crash between an assistant tool call and its results leaves dangling calls:
        # answer them as cancelled so providers accept the history. The results that did
        # arrive stay: their effects (edited files) are on disk either way.
        loaded.messages = close_dangling_calls(loaded.messages)
        return loaded

    @staticmethod
    def latest(home: Path, exclude: str | None = None) -> str | None:
        """The most recently active session that has messages (not ``exclude``)."""
        ids = active_sessions(home, exclude=exclude)
        return ids[0] if ids else None


def active_sessions(home: Path, exclude: str | None = None) -> list[str]:
    """Session ids with at least one message, most recently active first."""
    root = home / "sessions"
    if not root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for entry in root.iterdir():
        path = entry / "session.jsonl"
        if entry.name == exclude or ".agent-" in entry.name or not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                fh.readline()  # meta
                if not fh.readline().strip():
                    continue  # created but never used
            found.append((path.stat().st_mtime_ns, entry.name))
        except OSError:
            continue
    return [name for _, name in sorted(found, reverse=True)]


CANCELLED_RESULT = "[cancelled by the user before this call finished]"
# A loaded session's open calls: the process may have been killed after the call ran but
# before its result was written, so "cancelled" could make the model repeat a side effect.
UNKNOWN_RESULT = (
    "[the session stopped before this call's result was recorded: it may or may not have "
    "run. Check its effects (files, processes) before running it again]"
)


def close_dangling_calls(messages: list[Message]) -> list[Message]:
    """``messages`` with the last assistant turn's unanswered tool calls answered as of
    unknown outcome (results that did arrive are kept)."""
    out = list(messages)
    last = max((i for i, m in enumerate(out) if m.role == "assistant"), default=-1)
    if last < 0 or not out[last].tool_calls:
        return out
    answered = {m.tool_call_id for m in out[last + 1 :] if m.role == "tool"}
    for call in out[last].tool_calls:
        if call.id not in answered:
            out.append(
                Message(
                    "tool",
                    UNKNOWN_RESULT,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=True,
                )
            )
    return out


def resume_kwargs(home: Path, ref: str, *, exclude: str | None = None) -> dict[str, Any]:
    """Session keyword arguments that continue saved session ``ref`` (an id or 'last')."""
    session_id = SessionStore.latest(home, exclude=exclude) if ref == "last" else ref
    if not session_id:
        raise ValueError(f"no session to resume under {home}")
    loaded = SessionStore.load(home, session_id)
    return {
        "session_id": session_id,
        "messages": loaded.messages,
        "usage": loaded.usage,
        "cost_usd": loaded.cost_usd,
    }


def recent_sessions(home: Path, limit: int = 10) -> list[tuple[str, str]]:
    """(session id, first line of its first task), most recently active first."""
    root = home / "sessions"
    out: list[tuple[str, str]] = []
    for session_id in active_sessions(home)[:limit]:
        title = ""
        try:
            with (root / session_id / "session.jsonl").open(encoding="utf-8") as fh:
                for _, line in zip(range(200), fh, strict=False):
                    record = json.loads(line)
                    message = record.get("message") or {}
                    if record.get("kind") == "message" and message.get("role") == "user":
                        title = str(message.get("content") or "").strip().splitlines()[0][:100]
                        break
        except (OSError, ValueError, IndexError):
            pass
        out.append((session_id, title))
    return out
