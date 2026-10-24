"""One risk classifier and Smart Stop for every agent source.

KaroX is a control plane for agents that arrive from very different places: an
OpenAI or Anthropic API, a sponsor API, ChatGPT Web, Claude Web, an MCP client,
an external coding agent, a local native agent, or a KaroX subagent.

The rule this module exists to enforce is simple and absolute:

    **the source of an agent must never determine the level of safety.**

So there is no ChatGPT-Web risk check, no separate API risk check and no third
TUI risk check. Every mutating path funnels through :class:`RiskEngine`, which
answers two questions:

1. how dangerous is this exact action;
2. may it proceed right now, or must a human stop and look at it first.

The module never performs an action, never signals a process and never touches
a credential. It classifies, and it holds the confirmation ledger.

Confirmation contract (the part that is easy to get wrong)
----------------------------------------------------------

A confirmation is bound to one exact action digest, one session and one target.
It expires, it is single-use, and it is issued only by the human-facing layer.
The confirmation token never appears in an assessment payload, an event, an
evidence record or a tool result, so a model cannot read one, and a web page
cannot forge one. Change the action in any way and its digest changes, which
silently invalidates every confirmation already issued for the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class RiskLevel(str, Enum):
    """How much damage one action can do, ordered."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RANKS[self.value]

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank < other.rank


_RANKS = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class SmartStopRequired(PermissionError):
    """The action needs a human decision before it may proceed."""

    def __init__(self, assessment: "RiskAssessment") -> None:
        super().__init__(assessment.headline)
        self.assessment = assessment


class ConfirmationRejected(PermissionError):
    """A confirmation was missing, forged, stale, reused or for another action."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Rejection reasons are machine-stable; user-facing text lives in the catalogs.
REJECT_MISSING = "confirmation_missing"
REJECT_UNKNOWN = "confirmation_unknown"
REJECT_EXPIRED = "confirmation_expired"
REJECT_ALREADY_USED = "confirmation_already_used"
REJECT_WRONG_ACTION = "confirmation_wrong_action"
REJECT_WRONG_SESSION = "confirmation_wrong_session"

# Action kinds that are irreversible or reach outside the repository. These are
# never auto-approved, whatever profile or agent source asked for them.
CRITICAL_KINDS: frozenset[str] = frozenset(
    {
        "git.push",
        "git.force_push",
        "git.reset_hard",
        "git.clean",
        "git.rebase",
        "git.branch_delete",
        "git.tag_delete",
        "git.history_rewrite",
        "release.publish",
        "package.publish",
        "deploy",
        "account.delete",
        "account.security_change",
        "credential.export",
        "payment",
        "billing.change",
        "subscription.change",
        "system.path_write",
        "repository.escape",
    }
)

# Reaches outside the repository or changes access, but is not irreversible by
# construction.
HIGH_KINDS: frozenset[str] = frozenset(
    {
        "repo.delete",
        "repo.bulk_write",
        "repo.wide_replace",
        "credential.rotate",
        "permission.change",
        "deploy.prepare",
        "network.request",
        "browser.final_submit",
        "browser.send_message",
        "browser.grant_permission",
        "browser.upload",
        "process.run_unknown",
        "package.install",
        "migration.run",
    }
)

# Bounded, reversible, inside the repository.
MEDIUM_KINDS: frozenset[str] = frozenset(
    {
        "repo.write",
        "repo.create",
        "repo.move",
        "checks.run",
        "tests.run",
        "process.run_dev",
        "devserver.start",
        "browser.input",
        "git.commit",
        "mcp.call",
    }
)

# Observation only.
LOW_KINDS: frozenset[str] = frozenset(
    {
        "repo.read",
        "repo.search",
        "repo.list",
        "disk.read",
        "git.read",
        "status.read",
        "diagnostics.read",
        "browser.read",
        "browser.snapshot",
        "browser.screenshot",
    }
)

# A write that touches this many files at once stops being a bounded edit.
BULK_FILE_THRESHOLD = 10
# Only operations that actually mutate repository files participate in bulk-file
# escalation. Read/check/test payloads may legitimately name many paths; treating
# those targets as modified files turns a harmless verification run into a
# spurious Smart Stop at exactly BULK_FILE_THRESHOLD targets.
FILE_MUTATION_KINDS: frozenset[str] = frozenset(
    {
        "repo.write",
        "repo.create",
        "repo.move",
        "repo.delete",
        "repo.bulk_write",
        "repo.wide_replace",
        # A commit does not edit file contents, but its path set is still the exact
        # repository mutation scope a human is approving. Large commits therefore
        # keep Smart Stop, while checks/tests with many targets stay non-mutating.
        "git.commit",
    }
)
# Touching this share of the repository is a structural change, not an edit.
REPO_FRACTION_THRESHOLD = 0.25
# Deleting this many files at once always stops, regardless of share.
BULK_DELETE_THRESHOLD = 5
# How long a confirmation stays valid. Short on purpose: a human looked at a
# specific screen, and that context goes stale quickly.
DEFAULT_CONFIRMATION_TTL_SECONDS = 300.0
# Bounded previews keep a confirmation screen readable and an event small.
MAX_PREVIEW_PATHS = 20
MAX_TEXT = 500

# Paths that are outside any repository and must never be written blind.
_SYSTEM_PATH_HINTS = (
    re.compile(r"^[A-Za-z]:[\\/](windows|program files)", re.IGNORECASE),
    re.compile(r"^/(etc|bin|sbin|usr|boot|sys|proc)(/|$)"),
    re.compile(r"^/System(/|$)"),
)


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "\u2026"
    return text


def looks_like_system_path(path: str) -> bool:
    """True when a path escapes user space into an operating-system location."""

    candidate = str(path or "").strip()
    if not candidate:
        return False
    normalized = candidate.replace("\\", "/")
    for pattern in _SYSTEM_PATH_HINTS:
        if pattern.search(candidate) or pattern.search(normalized):
            return True
    return False


@dataclass(frozen=True)
class RiskAction:
    """One concrete thing an agent is about to do.

    ``source`` records where the agent came from purely for audit. It is
    deliberately excluded from the risk calculation and from the action digest:
    the same action must classify identically whether it arrives from an API
    model, ChatGPT Web, an MCP client or a subagent.
    """

    kind: str
    session_id: str
    summary: str = ""
    source: str = "unknown"
    target: str = ""
    paths: tuple[str, ...] = ()
    file_count: Optional[int] = None
    delete_count: int = 0
    create_count: int = 0
    modify_count: int = 0
    total_bytes: Optional[int] = None
    repository_file_count: Optional[int] = None
    recursive: bool = False
    irreversible: bool = False
    outside_repository: bool = False
    beyond_user_request: bool = False
    reversible_by_checkpoint: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def effective_file_count(self) -> int:
        if self.file_count is not None:
            return max(int(self.file_count), 0)
        counted = self.delete_count + self.create_count + self.modify_count
        return counted if counted else len(self.paths)

    @property
    def repository_fraction(self) -> Optional[float]:
        total = self.repository_file_count
        if not total or total <= 0:
            return None
        return min(self.effective_file_count / float(total), 1.0)

    def digest_payload(self) -> dict[str, Any]:
        """The canonical identity of this action.

        Anything that changes what actually happens belongs here. ``source``
        and ``summary`` do not: prose must never be able to invalidate or
        revalidate a human decision, and the agent's origin must not change the
        identity of an otherwise identical action.
        """

        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "target": self.target,
            "paths": sorted(self.paths),
            "file_count": self.effective_file_count,
            "delete_count": self.delete_count,
            "create_count": self.create_count,
            "modify_count": self.modify_count,
            "total_bytes": self.total_bytes,
            "recursive": self.recursive,
            "irreversible": self.irreversible,
            "outside_repository": self.outside_repository,
            "details": _canonical(self.details),
        }

    @property
    def digest(self) -> str:
        blob = json.dumps(
            self.digest_payload(), sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class RiskAssessment:
    """The verdict for one action, safe to show, log and export."""

    action_digest: str
    kind: str
    session_id: str
    level: RiskLevel
    requires_confirmation: bool
    reasons: tuple[str, ...]
    headline: str
    target: str = ""
    preview: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocked_without_user(self) -> bool:
        return self.requires_confirmation

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["level"] = self.level.value
        payload["reasons"] = list(self.reasons)
        payload["preview"] = dict(self.preview)
        return payload


@dataclass(frozen=True)
class ConfirmationGrant:
    """Proof that a human approved one exact action.

    ``token`` is the only secret in this module. It is returned to the caller
    that asked the human, and it is never placed in an assessment, an event or
    an evidence record.
    """

    token: str
    action_digest: str
    session_id: str
    issued_at: float
    expires_at: float

    def public_record(self) -> dict[str, Any]:
        """A secret-free description, which is what audit and evidence store."""

        return {
            "action_digest": self.action_digest,
            "session_id": self.session_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


class ConfirmationLedger:
    """Single-use, time-bounded, action-bound human confirmations.

    Tokens are stored hashed and compared in constant time, so a leaked ledger
    file cannot be replayed and a guessed prefix cannot be probed by timing.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        now: Any = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")
        self._ttl = float(ttl_seconds)
        self._now = now
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        assessment: RiskAssessment,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> ConfirmationGrant:
        """Record a human decision. Only the user-facing layer may call this."""

        token = secrets.token_urlsafe(32)
        issued = float(self._now())
        ttl = float(ttl_seconds if ttl_seconds is not None else self._ttl)
        if ttl <= 0:
            raise ValueError("confirmation TTL must be positive")
        grant = ConfirmationGrant(
            token=token,
            action_digest=assessment.action_digest,
            session_id=assessment.session_id,
            issued_at=issued,
            expires_at=issued + ttl,
        )
        with self._lock:
            self._entries[self._hash(token)] = {
                "action_digest": grant.action_digest,
                "session_id": grant.session_id,
                "expires_at": grant.expires_at,
                "used": False,
            }
        return grant

    def redeem(self, token: Optional[str], assessment: RiskAssessment) -> None:
        """Consume a confirmation, or raise :class:`ConfirmationRejected`.

        Every rejection path is deliberate:

        * no token at all -- a model decided to proceed by itself;
        * unknown token -- forged, or scraped from page content or tool output;
        * expired -- the human looked at a screen that is no longer current;
        * already used -- a replayed approval, which is how one "yes" turns
          into a loop of mutations;
        * wrong action -- the action changed after approval;
        * wrong session -- an approval borrowed from another session.
        """

        if not token or not isinstance(token, str):
            raise ConfirmationRejected(REJECT_MISSING)
        digest = self._hash(token)
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                raise ConfirmationRejected(REJECT_UNKNOWN)
            if entry["used"]:
                raise ConfirmationRejected(REJECT_ALREADY_USED)
            if float(self._now()) > float(entry["expires_at"]):
                # Drop it so an expired approval cannot linger in memory.
                self._entries.pop(digest, None)
                raise ConfirmationRejected(REJECT_EXPIRED)
            if not hmac.compare_digest(
                str(entry["action_digest"]), assessment.action_digest
            ):
                raise ConfirmationRejected(REJECT_WRONG_ACTION)
            if not hmac.compare_digest(
                str(entry["session_id"]), assessment.session_id
            ):
                raise ConfirmationRejected(REJECT_WRONG_SESSION)
            entry["used"] = True

    def purge_expired(self) -> int:
        now = float(self._now())
        with self._lock:
            stale = [
                key
                for key, entry in self._entries.items()
                if entry["used"] or float(entry["expires_at"]) < now
            ]
            for key in stale:
                self._entries.pop(key, None)
            return len(stale)


def build_bulk_preview(action: RiskAction) -> dict[str, Any]:
    """A bounded, secret-free summary a human can actually judge."""

    paths = list(action.paths)[:MAX_PREVIEW_PATHS]
    directories = sorted(
        {
            item.replace("\\", "/").rsplit("/", 1)[0]
            for item in action.paths
            if "/" in item.replace("\\", "/")
        }
    )[:MAX_PREVIEW_PATHS]
    fraction = action.repository_fraction
    return {
        "file_count": action.effective_file_count,
        "sample_paths": paths,
        "sample_truncated": len(action.paths) > len(paths),
        "directories": directories,
        "deletions": action.delete_count,
        "creations": action.create_count,
        "modifications": action.modify_count,
        "total_bytes": action.total_bytes,
        "repository_fraction": round(fraction, 4) if fraction is not None else None,
        "recursive": action.recursive,
        "rollback_available": action.reversible_by_checkpoint,
    }


class RiskEngine:
    """Classify any action from any agent source, and hold the stop line."""

    def __init__(
        self,
        *,
        ledger: Optional[ConfirmationLedger] = None,
        auto_approve_up_to: RiskLevel = RiskLevel.MEDIUM,
        bulk_file_threshold: int = BULK_FILE_THRESHOLD,
        bulk_delete_threshold: int = BULK_DELETE_THRESHOLD,
        repo_fraction_threshold: float = REPO_FRACTION_THRESHOLD,
    ) -> None:
        if auto_approve_up_to.rank >= RiskLevel.HIGH.rank:
            # A configuration that auto-approves high risk would quietly delete
            # the whole point of Smart Stop, so it is refused at construction.
            raise ValueError("high and critical actions always need a human")
        self.ledger = ledger or ConfirmationLedger()
        self._auto_approve_up_to = auto_approve_up_to
        self._bulk_files = int(bulk_file_threshold)
        self._bulk_deletes = int(bulk_delete_threshold)
        self._repo_fraction = float(repo_fraction_threshold)

    def _base_level(self, action: RiskAction) -> tuple[RiskLevel, list[str]]:
        kind = action.kind
        if kind in CRITICAL_KINDS:
            return RiskLevel.CRITICAL, [f"kind:{kind}"]
        if kind in HIGH_KINDS:
            return RiskLevel.HIGH, [f"kind:{kind}"]
        if kind in MEDIUM_KINDS:
            return RiskLevel.MEDIUM, [f"kind:{kind}"]
        if kind in LOW_KINDS:
            return RiskLevel.LOW, []
        # An unrecognised action is not a safe action. Treating unknown as high
        # is what keeps a newly added tool from bypassing Smart Stop by simply
        # not being in a list yet.
        return RiskLevel.HIGH, ["kind:unknown"]

    def assess(self, action: RiskAction) -> RiskAssessment:
        level, reasons = self._base_level(action)

        def raise_to(candidate: RiskLevel, reason: str) -> None:
            nonlocal level
            if candidate.rank > level.rank:
                level = candidate
            if reason not in reasons:
                reasons.append(reason)

        if action.irreversible:
            raise_to(RiskLevel.CRITICAL, "irreversible")
        if action.outside_repository:
            raise_to(RiskLevel.CRITICAL, "outside_repository")
        if any(looks_like_system_path(item) for item in action.paths):
            raise_to(RiskLevel.CRITICAL, "system_path")
        if looks_like_system_path(action.target):
            raise_to(RiskLevel.CRITICAL, "system_path")

        if action.recursive and action.delete_count:
            raise_to(RiskLevel.HIGH, "recursive_delete")
        if action.delete_count >= self._bulk_deletes:
            raise_to(RiskLevel.HIGH, "bulk_delete")
        if action.kind in FILE_MUTATION_KINDS:
            if action.effective_file_count >= self._bulk_files:
                raise_to(RiskLevel.HIGH, "bulk_mutation")
            fraction = action.repository_fraction
            if fraction is not None and fraction >= self._repo_fraction:
                raise_to(RiskLevel.HIGH, "large_repository_share")
        if action.beyond_user_request:
            # Doing noticeably more than was asked is its own hazard, even when
            # each individual step looks harmless.
            raise_to(RiskLevel.HIGH, "beyond_user_request")

        requires_confirmation = level.rank > self._auto_approve_up_to.rank
        preview: Mapping[str, Any] = {}
        if action.effective_file_count or action.paths:
            preview = build_bulk_preview(action)

        return RiskAssessment(
            action_digest=action.digest,
            kind=action.kind,
            session_id=action.session_id,
            level=level,
            requires_confirmation=requires_confirmation,
            reasons=tuple(reasons),
            headline=_clip(action.summary) or action.kind,
            target=_clip(action.target, 200),
            preview=preview,
        )

    def authorize(
        self,
        action: RiskAction,
        *,
        confirmation_token: Optional[str] = None,
    ) -> RiskAssessment:
        """Return the assessment, or refuse to let the action proceed.

        A model calling this with no token gets :class:`SmartStopRequired`. A
        model that invents, replays or borrows a token gets
        :class:`ConfirmationRejected`. There is no third path.
        """

        assessment = self.assess(action)
        if not assessment.requires_confirmation:
            return assessment
        if confirmation_token is None:
            raise SmartStopRequired(assessment)
        self.ledger.redeem(confirmation_token, assessment)
        return assessment


_ENGINE: Optional[RiskEngine] = None
_ENGINE_LOCK = threading.Lock()


def risk_engine() -> RiskEngine:
    """The process-wide engine, so one ledger governs every agent source."""

    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RiskEngine()
        return _ENGINE


__all__ = [
    "BULK_DELETE_THRESHOLD",
    "BULK_FILE_THRESHOLD",
    "CRITICAL_KINDS",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "HIGH_KINDS",
    "LOW_KINDS",
    "MEDIUM_KINDS",
    "REJECT_ALREADY_USED",
    "REJECT_EXPIRED",
    "REJECT_MISSING",
    "REJECT_UNKNOWN",
    "REJECT_WRONG_ACTION",
    "REJECT_WRONG_SESSION",
    "REPO_FRACTION_THRESHOLD",
    "ConfirmationGrant",
    "ConfirmationLedger",
    "ConfirmationRejected",
    "RiskAction",
    "RiskAssessment",
    "RiskEngine",
    "RiskLevel",
    "SmartStopRequired",
    "build_bulk_preview",
    "looks_like_system_path",
    "risk_engine",
]
