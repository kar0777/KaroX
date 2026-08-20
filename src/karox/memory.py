"""KaroXMemory: one local memory layer for every client and provider.

Models change, clients change; what the user taught the runtime stays here.
This is deliberately *not* task state under a new name: task_state.py tracks
one workstream's execution facts, while this layer holds durable knowledge in
four scopes (USER, PROJECT, WORKSTREAM, SESSION) that any client -- native
agent, hosted MCP guest, or API provider -- can read through one API.

Design contract:

* local only: plain JSON files under an explicit root, no network;
* inspectable: every entry is readable and carries provenance;
* forgettable: forget() removes entries permanently, on disk, immediately;
* scope-isolated: one scope's file never contains another scope's entries;
* secret-free: content that looks like a credential is refused, never stored;
* honest retrieval: deterministic Unicode-aware lexical ranking (token
  overlap plus a small curated multilingual alias map) under a byte budget,
  never a memory dump.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import unicodedata
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    WORKSTREAM = "workstream"
    SESSION = "session"


class MemoryKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    NOTE = "note"
    TODO = "todo"
    HANDOFF = "handoff"


class MemoryError(RuntimeError):
    """Invalid input to the memory layer."""


class MemoryPolicyError(MemoryError):
    """Content the memory layer refuses to store."""


# Credential-shaped content is rejected outright. The patterns are broad on
# purpose: a false rejection costs one manual rephrase, a false acceptance
# writes a secret to disk in plain text.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(password|passwd|api[_-]?key|secret|token|bearer|authorization|"
        r"private[_-]?key|client[_-]?secret|access[_-]?key|refresh[_-]?token|"
        r"recovery[_-]?code|card[_-]?number|cvv|cvc)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk|pk|ghp|gho|xoxb|xoxp|ya29)[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

# Unicode-aware tokenization: letters and digits from any script count, so
# Cyrillic, Latin, and mixed-language queries rank identically. Text is
# NFKC-normalized and casefolded first; Russian ё collapses to е. Tokens that
# contain underscores (snake_case keys) also contribute their parts, so a
# query saying "name" can reach the key "preferred_name".
_WORD = re.compile(r"\w{2,}")

_SENSITIVITIES = frozenset({"normal", "personal"})

# Deterministic multilingual aliases: a small curated concept map so a
# Russian question reaches an English-keyed entry (and vice versa) without an
# embedding subsystem. Keys must already be normalized (casefold, ё→е).
# Extend deliberately; keep it small, reviewable, and deterministic.
_CONCEPT_ALIASES: dict[str, str] = {
    # the "name" concept
    "name": "name",
    "named": "name",
    "nickname": "name",
    "имя": "name",
    "имени": "name",
    "именем": "name",
    "зовут": "name",
    "зовет": "name",
    "звать": "name",
    "называть": "name",
    "называют": "name",
    "называюсь": "name",
    "называется": "name",
    "никнейм": "name",
    # the "preference" concept
    "prefer": "preference",
    "prefers": "preference",
    "preferred": "preference",
    "preference": "preference",
    "preferences": "preference",
    "предпочитает": "preference",
    "предпочитаю": "preference",
    "предпочтение": "preference",
    "предпочтения": "preference",
    # the "user"/self concept
    "user": "user",
    "my": "user",
    "me": "user",
    "пользователь": "user",
    "пользователя": "user",
    "пользователю": "user",
    "меня": "user",
    "мне": "user",
    "мое": "user",
    "мой": "user",
    "моя": "user",
}


def _now() -> float:
    return time.time()


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _WORD.finditer(_normalize(text)):
        token = match.group(0)
        tokens.add(token)
        if "_" in token:
            tokens.update(part for part in token.split("_") if len(part) >= 2)
    return tokens


def _concepts(tokens: set[str]) -> set[str]:
    return {_CONCEPT_ALIASES[token] for token in tokens if token in _CONCEPT_ALIASES}


def reject_secret_like(content: str) -> None:
    """Raise MemoryPolicyError when content looks like a credential."""

    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            raise MemoryPolicyError(
                "memory refuses credential-shaped content; store a reference "
                "to where the secret lives instead of the secret itself"
            )


@dataclasses.dataclass(frozen=True)
class MemoryEntry:
    """One durable, provenance-carrying piece of knowledge."""

    entry_id: str
    scope: MemoryScope
    scope_id: str
    kind: MemoryKind
    content: str
    key: Optional[str] = None
    provenance: str = "unknown"
    confidence: float = 0.8
    sensitivity: str = "normal"
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: Optional[float] = None
    ttl_seconds: Optional[float] = None
    validation: str = "valid"
    source_path: Optional[str] = None
    source_sha256: Optional[str] = None

    def expired(self, *, now: Optional[float] = None) -> bool:
        if self.ttl_seconds is None:
            return False
        return (self.created_at + self.ttl_seconds) <= (now or _now())

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["scope"] = self.scope.value
        payload["kind"] = self.kind.value
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MemoryEntry":
        return cls(
            entry_id=str(payload["entry_id"]),
            scope=MemoryScope(str(payload["scope"])),
            scope_id=str(payload["scope_id"]),
            kind=MemoryKind(str(payload["kind"])),
            content=str(payload["content"]),
            key=payload.get("key"),
            provenance=str(payload.get("provenance", "unknown")),
            confidence=float(payload.get("confidence", 0.8)),
            sensitivity=str(payload.get("sensitivity", "normal")),
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
            last_used_at=payload.get("last_used_at"),
            ttl_seconds=payload.get("ttl_seconds"),
            validation=str(payload.get("validation", "valid")),
            source_path=payload.get("source_path"),
            source_sha256=payload.get("source_sha256"),
        )


class KaroXMemory:
    """Scope-isolated local memory store with budgeted deterministic recall."""

    SCHEMA_VERSION = 1

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- storage -----------------------------------------------------------

    def _file(self, scope: MemoryScope, scope_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", scope_id) or "default"
        return self.root / f"{scope.value}-{safe}.json"

    def _load(self, scope: MemoryScope, scope_id: str) -> list[MemoryEntry]:
        path = self._file(scope, scope_id)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = []
        for item in payload.get("entries", []):
            try:
                entry = MemoryEntry.from_dict(item)
            except (KeyError, ValueError):
                continue
            if not entry.expired():
                entries.append(entry)
        return entries

    def _save(
        self, scope: MemoryScope, scope_id: str, entries: Iterable[MemoryEntry]
    ) -> None:
        path = self._file(scope, scope_id)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "scope": scope.value,
            "scope_id": scope_id,
            "entries": [entry.to_dict() for entry in entries],
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    # -- API -----------------------------------------------------------------

    def remember(
        self,
        *,
        scope: MemoryScope,
        kind: MemoryKind,
        content: str,
        scope_id: str = "default",
        key: Optional[str] = None,
        provenance: str = "unknown",
        confidence: float = 0.8,
        sensitivity: str = "normal",
        ttl_seconds: Optional[float] = None,
        source_path: Optional[str] = None,
        source_sha256: Optional[str] = None,
    ) -> MemoryEntry:
        text = content.strip()
        if not text:
            raise MemoryError("memory content must be a non-empty string")
        if len(text) > 4000:
            raise MemoryError("memory content is capped at 4000 characters")
        if sensitivity not in _SENSITIVITIES:
            raise MemoryError(f"sensitivity must be one of {sorted(_SENSITIVITIES)}")
        if not 0.0 <= confidence <= 1.0:
            raise MemoryError("confidence must be between 0 and 1")
        reject_secret_like(text)
        if key is not None:
            reject_secret_like(key)
        now = _now()
        entries = self._load(scope, scope_id)
        if key is not None:
            # A keyed remember is an upsert: the newest statement of a fact
            # replaces the old one instead of accumulating contradictions.
            entries = [item for item in entries if item.key != key]
        entry = MemoryEntry(
            entry_id=f"mem-{uuid.uuid4().hex[:20]}",
            scope=scope,
            scope_id=scope_id,
            kind=kind,
            content=text,
            key=key,
            provenance=provenance,
            confidence=confidence,
            sensitivity=sensitivity,
            created_at=now,
            updated_at=now,
            ttl_seconds=ttl_seconds,
            source_path=source_path,
            source_sha256=source_sha256,
        )
        entries.append(entry)
        self._save(scope, scope_id, entries)
        return entry

    def list(
        self, *, scope: MemoryScope, scope_id: str = "default"
    ) -> tuple[MemoryEntry, ...]:
        return tuple(self._load(scope, scope_id))

    def forget(
        self,
        *,
        scope: MemoryScope,
        scope_id: str = "default",
        entry_id: Optional[str] = None,
        key: Optional[str] = None,
    ) -> int:
        if entry_id is None and key is None:
            raise MemoryError("forget requires entry_id or key")
        entries = self._load(scope, scope_id)
        kept = [
            item
            for item in entries
            if not (
                (entry_id is not None and item.entry_id == entry_id)
                or (key is not None and item.key == key)
            )
        ]
        removed = len(entries) - len(kept)
        if removed:
            self._save(scope, scope_id, kept)
        return removed

    def recall(
        self,
        *,
        query: str,
        scopes: Iterable[tuple[MemoryScope, str]],
        limit: int = 5,
        budget_chars: int = 2000,
        include_personal: bool = True,
    ) -> tuple[MemoryEntry, ...]:
        """Deterministic ranked recall: overlap, recency, confidence, budget."""

        query_tokens = _tokens(query)
        query_concepts = _concepts(query_tokens)
        normalized_query = _normalize(query)
        now = _now()
        scored: list[tuple[float, MemoryEntry]] = []
        for scope, scope_id in scopes:
            for entry in self._load(scope, scope_id):
                if entry.sensitivity == "personal" and not include_personal:
                    continue
                content_tokens = _tokens(entry.content)
                key_tokens = _tokens(entry.key) if entry.key is not None else set()
                content_overlap = len(query_tokens & content_tokens)
                key_overlap = len(query_tokens & key_tokens)
                concept_overlap = len(
                    query_concepts & _concepts(content_tokens | key_tokens)
                )
                exact_key = (
                    entry.key is not None
                    and _normalize(entry.key) in normalized_query
                )
                evidence = content_overlap + key_overlap + concept_overlap
                if query_tokens and not evidence and not exact_key:
                    continue
                age_days = max(0.0, (now - entry.updated_at) / 86_400.0)
                recency = 1.0 / (1.0 + age_days)
                score = (
                    content_overlap * 10.0
                    + key_overlap * 15.0
                    + concept_overlap * 6.0
                    + (20.0 if exact_key else 0.0)
                    + recency * 2.0
                    + entry.confidence
                )
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1].entry_id))
        selected: list[MemoryEntry] = []
        used = 0
        for _score, entry in scored:
            if len(selected) >= limit:
                break
            cost = len(entry.content)
            if used + cost > budget_chars and selected:
                continue
            selected.append(entry)
            used += cost
        self._touch(selected, now)
        return tuple(selected)

    def context(
        self,
        *,
        scopes: Iterable[tuple[MemoryScope, str]],
        budget_chars: int = 1500,
        task: str = "",
    ) -> str:
        """A compact, prompt-ready bundle; empty string when nothing relevant."""

        entries = self.recall(
            query=task or "",
            scopes=tuple(scopes),
            limit=12,
            budget_chars=budget_chars,
        )
        if not entries:
            return ""
        lines = ["Relevant KaroX memory (local, user-controlled):"]
        for entry in entries:
            lines.append(
                f"- [{entry.scope.value}/{entry.kind.value}] {entry.content}"
            )
        return "\n".join(lines)

    # -- revalidation --------------------------------------------------------

    def mark_stale(self, *, source_path: str) -> int:
        """Flag every source-backed entry for a path as stale, all scopes."""

        flagged = 0
        for file in self.root.glob("*.json"):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            scope = MemoryScope(str(payload.get("scope", "session")))
            scope_id = str(payload.get("scope_id", "default"))
            entries = self._load(scope, scope_id)
            changed = False
            updated: list[MemoryEntry] = []
            for entry in entries:
                if entry.source_path == source_path and entry.validation == "valid":
                    updated.append(
                        dataclasses.replace(
                            entry, validation="stale", updated_at=_now()
                        )
                    )
                    flagged += 1
                    changed = True
                else:
                    updated.append(entry)
            if changed:
                self._save(scope, scope_id, updated)
        return flagged

    def revalidate(
        self,
        *,
        entry_id: str,
        scope: MemoryScope,
        scope_id: str = "default",
        current_sha256: str,
    ) -> Optional[MemoryEntry]:
        """Confirm or refresh a source-backed entry against the current hash."""

        entries = self._load(scope, scope_id)
        updated: list[MemoryEntry] = []
        result: Optional[MemoryEntry] = None
        for entry in entries:
            if entry.entry_id == entry_id:
                validation = (
                    "valid" if entry.source_sha256 == current_sha256 else "stale"
                )
                result = dataclasses.replace(
                    entry, validation=validation, updated_at=_now()
                )
                updated.append(result)
            else:
                updated.append(entry)
        if result is not None:
            self._save(scope, scope_id, updated)
        return result

    def _touch(self, entries: Iterable[MemoryEntry], now: float) -> None:
        by_file: dict[tuple[MemoryScope, str], list[str]] = {}
        for entry in entries:
            by_file.setdefault((entry.scope, entry.scope_id), []).append(
                entry.entry_id
            )
        for (scope, scope_id), ids in by_file.items():
            stored = self._load(scope, scope_id)
            refreshed = [
                dataclasses.replace(item, last_used_at=now)
                if item.entry_id in ids
                else item
                for item in stored
            ]
            self._save(scope, scope_id, refreshed)
