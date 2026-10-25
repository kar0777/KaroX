"""One-shot, action-bound approvals for consequential desktop actions.

Launching applications and putting a window on the user's screen are visible,
consequential acts. They proceed only with a single-use token that a human
issued for the *exact* action digest, mirroring the fail-closed semantics of
:mod:`karox.risk_engine`'s confirmation ledger. The store is durable (JSON
under the runtime dir) because the CLI path is one process per tool call; only
token hashes are ever written to disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .errors import ComputerUseError

DEFAULT_APPROVAL_TTL_SECONDS = 300.0

REJECT_MISSING = "approval_missing"
REJECT_UNKNOWN = "approval_unknown"
REJECT_EXPIRED = "approval_expired"
REJECT_ALREADY_USED = "approval_already_used"
REJECT_WRONG_ACTION = "approval_wrong_action"
REJECT_WRONG_SESSION = "approval_wrong_session"

#: Actions that put something new on the user's screen or start a process.
CONSEQUENTIAL_ACTIONS = frozenset(
    {
        "desktop.launch_app",
        "desktop.open_terminal",
        "desktop.show_window",
    }
)

#: Payload defaults folded in before digesting, so an approval covers the
#: exact effective action whether or not the caller spelled out defaults.
PAYLOAD_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "desktop.open_terminal": {"shell": "auto", "keep_open": True},
    "desktop.show_window": {"mode": "restore_noactivate"},
    "desktop.click": {"button": "left", "clicks": 1},
    "desktop.scroll": {"clicks": -3},
    "desktop.capture": {"max_dim": 1800},
    "desktop.attach": {"access": "observe"},
    "desktop.list_windows": {"limit": 80},
}


class ApprovalRejected(ComputerUseError):
    """A confirmation token was missing, forged, stale, replayed or misbound."""

    error_type = "approval_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(f"desktop action approval rejected: {reason}")
        self.reason = reason


def normalize_payload(action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    defaults = dict(PAYLOAD_DEFAULTS.get(action, {}))
    defaults.update({k: v for k, v in payload.items() if k != "confirmation_token"})
    return defaults


def action_digest(action: str, payload: Mapping[str, Any]) -> str:
    """The canonical identity of one exact desktop action."""

    canonical = json.dumps(
        {"action": action, "payload": normalize_payload(action, payload)},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_consequential(action: str, payload: Mapping[str, Any]) -> bool:
    """Whether one action needs a human one-shot approval.

    A minimized/no-activate window restore does not occupy the user's screen,
    so it stays an ordinary action; asking for a *visible* restore does.
    """

    if action in ("desktop.launch_app", "desktop.open_terminal"):
        return True
    if action == "desktop.show_window":
        mode = normalize_payload(action, payload).get("mode", "restore_noactivate")
        return mode == "restore_visible"
    return False




class ApprovalStore:
    """Durable single-use tokens, stored hashed, bound to action and session."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("approval TTL must be positive")
        self._path = Path(path)
        self._now = now
        self._ttl = float(ttl_seconds)

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing or corrupt store: fail closed as "no approvals exist".
            return {"entries": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            return {"entries": {}}
        return {"entries": raw["entries"]}

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".approvals-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _purge(self, entries: dict[str, Any]) -> dict[str, Any]:
        now = float(self._now())
        return {
            key: entry
            for key, entry in entries.items()
            if not entry.get("used") and float(entry.get("expires_at", 0.0)) >= now
        }


    def issue(
        self,
        digest: str,
        *,
        session_id: str,
        ttl_seconds: Optional[float] = None,
        headline: str = "",
    ) -> str:
        """Record a human decision. Only a human-facing layer may call this."""

        token = secrets.token_urlsafe(32)
        issued = float(self._now())
        ttl = float(ttl_seconds if ttl_seconds is not None else self._ttl)
        if ttl <= 0:
            raise ValueError("approval TTL must be positive")
        state = self._load()
        entries = self._purge(state["entries"])
        entries[self._hash(token)] = {
            "action_digest": str(digest),
            "session_id": str(session_id),
            "issued_at": issued,
            "expires_at": issued + ttl,
            "used": False,
            "headline": str(headline)[:400],
        }
        self._save({"entries": entries})
        return token

    def redeem(self, token: Optional[str], digest: str, session_id: str) -> None:
        """Consume one approval or raise :class:`ApprovalRejected`.

        Rejection paths match the risk-engine ledger: missing, unknown,
        expired, replayed, wrong action, wrong session. There is no path where
        a model can approve itself.
        """

        if not token or not isinstance(token, str):
            raise ApprovalRejected(REJECT_MISSING)
        key = self._hash(token)
        state = self._load()
        entries = state["entries"]
        entry = entries.get(key)
        if entry is None:
            raise ApprovalRejected(REJECT_UNKNOWN)
        if entry.get("used"):
            raise ApprovalRejected(REJECT_ALREADY_USED)
        if float(self._now()) > float(entry.get("expires_at", 0.0)):
            entries.pop(key, None)
            self._save({"entries": self._purge(entries)})
            raise ApprovalRejected(REJECT_EXPIRED)
        if not hmac.compare_digest(str(entry.get("action_digest", "")), str(digest)):
            raise ApprovalRejected(REJECT_WRONG_ACTION)
        if not hmac.compare_digest(str(entry.get("session_id", "")), str(session_id)):
            raise ApprovalRejected(REJECT_WRONG_SESSION)
        entry["used"] = True
        self._save({"entries": entries})


__all__ = [
    "CONSEQUENTIAL_ACTIONS",
    "ApprovalRejected",
    "ApprovalStore",
    "action_digest",
    "is_consequential",
    "normalize_payload",
]
