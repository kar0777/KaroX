"""Session persistence: one append-only JSONL file per session, enough to resume.

Records: ``meta`` (once), ``message``, ``usage``, ``reset`` (full message list after a
compaction), ``event`` (run_finished). Loading replays them in order.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.core.messages import Message, Usage


def default_home() -> Path:
    env = os.environ.get("KAROX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".karox6"


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


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
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def message(self, message: Message) -> None:
        self.append("message", message=message.to_dict())

    def reset(self, messages: list[Message]) -> None:
        self.append("reset", messages=[m.to_dict() for m in messages])

    @staticmethod
    def load(home: Path, session_id: str) -> LoadedSession:
        path = home / "sessions" / session_id / "session.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"no session {session_id!r} under {home / 'sessions'}")
        loaded = LoadedSession(session_id=session_id, meta={})
        for line in path.read_text(encoding="utf-8").splitlines():
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
        # drop the incomplete tail so providers accept the history.
        loaded.messages = drop_dangling_calls(loaded.messages)
        return loaded

    @staticmethod
    def latest(home: Path) -> str | None:
        root = home / "sessions"
        if not root.is_dir():
            return None
        ids = sorted(p.name for p in root.iterdir() if (p / "session.jsonl").is_file())
        return ids[-1] if ids else None


def drop_dangling_calls(messages: list[Message]) -> list[Message]:
    out = list(messages)
    while out:
        last_assistant = max(
            (i for i, m in enumerate(out) if m.role == "assistant" and m.tool_calls), default=-1
        )
        if last_assistant < 0:
            break
        wanted = {c.id for c in out[last_assistant].tool_calls}
        have = {m.tool_call_id for m in out[last_assistant + 1 :] if m.role == "tool"}
        if wanted <= have:
            break
        out = out[:last_assistant]
    return out


def resume_kwargs(home: Path, ref: str) -> dict[str, Any]:
    """Session keyword arguments that continue saved session ``ref`` (an id or 'last')."""
    session_id = SessionStore.latest(home) if ref == "last" else ref
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
    """(session id, first line of its first task), newest first."""
    root = home / "sessions"
    if not root.is_dir():
        return []
    ids = sorted((p.name for p in root.iterdir() if (p / "session.jsonl").is_file()), reverse=True)
    out: list[tuple[str, str]] = []
    for session_id in ids[:limit]:
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
