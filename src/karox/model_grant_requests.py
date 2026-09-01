"""Pending user-approval requests for model delegation grants.

Hosted agents may create and inspect *requests*.  Requests have zero authority.
Only a trusted local UI (TUI/mobile) or a future host channel with unforgeable
user-origin metadata may call :meth:`ModelGrantRequestStore.approve` with a
:class:`~karox.model_grants.LocalGrantApproval`.

The approval may narrow numeric limits but can never widen what the agent asked
for.  This gives the local surface a simple "Allow / lower limits / deny" UX and
keeps the security boundary independent of wording in the hosted conversation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

from .model_grants import (
    GrantScope,
    LocalGrantApproval,
    ModelGrant,
    ModelGrantStore,
    ModelSelector,
)
from .paths import runtime_dir

_SCHEMA_VERSION = 1


class GrantRequestError(RuntimeError):
    pass


class GrantRequestDenied(GrantRequestError):
    pass


@dataclasses.dataclass(frozen=True)
class ModelGrantRequest:
    request_id: str
    requester_id: str
    scope: GrantScope
    selectors: tuple[ModelSelector, ...]
    free_only: bool
    max_cost_microusd: int
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_request_output_tokens: int
    max_parallel: int
    allow_fallback: bool
    grant_ttl_seconds: int
    request_expires_at: float
    max_price_age_seconds: float
    created_at: float
    label: str = ""

    def __post_init__(self) -> None:
        _text(self.request_id, "request id", 256)
        _text(self.requester_id, "requester id", 256)
        if not self.selectors:
            raise ValueError("grant request requires at least one selector")
        if not isinstance(self.free_only, bool) or not isinstance(self.allow_fallback, bool):
            raise ValueError("grant request flags must be booleans")
        for name, value in (
            ("max_cost_microusd", self.max_cost_microusd),
            ("max_calls", self.max_calls),
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_request_output_tokens", self.max_request_output_tokens),
            ("max_parallel", self.max_parallel),
            ("grant_ttl_seconds", self.grant_ttl_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if min(
            self.max_calls,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_request_output_tokens,
            self.max_parallel,
            self.grant_ttl_seconds,
        ) < 1:
            raise ValueError("grant request budgets and TTL must be positive")
        if self.max_request_output_tokens > self.max_output_tokens:
            raise ValueError("grant request per-call output exceeds total output")
        if self.free_only and self.max_cost_microusd != 0:
            raise ValueError("free-only request must have zero money budget")
        if not 60 <= self.grant_ttl_seconds <= 86_400:
            raise ValueError("grant TTL must be between 1 minute and 24 hours")
        if not self.request_expires_at > self.created_at:
            raise ValueError("approval request expiry must be after creation")
        if not 1 <= float(self.max_price_age_seconds) <= 86_400:
            raise ValueError("pricing proof age must be between 1 second and 24 hours")
        if self.label:
            _text(self.label, "grant request label", 256)


@dataclasses.dataclass(frozen=True)
class ApprovalLimits:
    """Optional local reductions to the agent-proposed budget."""

    max_cost_microusd: Optional[int] = None
    max_calls: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_request_output_tokens: Optional[int] = None
    max_parallel: Optional[int] = None
    grant_ttl_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} reduction must be a non-negative integer")


@dataclasses.dataclass(frozen=True)
class GrantRequestStatus:
    request: ModelGrantRequest
    state: str
    grant_id: Optional[str] = None
    decision_reason: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.state in {"approved", "denied", "expired"}


class ModelGrantRequestStore:
    """Small cross-process queue between hosted requesters and local approval UI."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        grant_store: Optional[ModelGrantStore] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(
            path or (runtime_dir() / "vnext" / "model-delegation-requests.sqlite3")
        ).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.grant_store = grant_store or ModelGrantStore(clock=clock)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version is None or version["value"] != str(_SCHEMA_VERSION):
                raise GrantRequestError("grant request store has unsupported schema")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    requester_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    grant_id TEXT,
                    decision_reason TEXT,
                    decided_at REAL,
                    UNIQUE(requester_id, idempotency_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_grant_requests_state ON requests(state)"
            )

    def create(self, request: ModelGrantRequest, *, idempotency_key: str) -> GrantRequestStatus:
        _text(idempotency_key, "grant request idempotency key", 256)
        payload = _request_json(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM requests WHERE requester_id=? AND idempotency_key=?",
                    (request.requester_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    prior = _request_from_json(str(existing["payload"]))
                    if prior != request:
                        raise GrantRequestError(
                            "grant request idempotency key was reused with different authority"
                        )
                    connection.commit()
                    return self._status_from_row(existing)
                connection.execute(
                    """
                    INSERT INTO requests(request_id, requester_id, idempotency_key, payload, state)
                    VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (request.request_id, request.requester_id, idempotency_key, payload),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.status(request.request_id)

    def status(self, request_id: str) -> GrantRequestStatus:
        _text(request_id, "grant request id", 256)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request_id)
            request = _request_from_json(str(row["payload"]))
            if row["state"] == "pending" and float(self.clock()) >= request.request_expires_at:
                connection.execute(
                    "UPDATE requests SET state='expired', decision_reason='approval_timeout', decided_at=? WHERE request_id=?",
                    (float(self.clock()), request_id),
                )
                connection.commit()
                row = self._row(connection, request_id)
            else:
                connection.commit()
            return self._status_from_row(row)

    def pending(self) -> tuple[GrantRequestStatus, ...]:
        # Normalize expiries first so a local approval screen never offers a
        # stale request merely because nobody polled it from the hosted side.
        with self._connect() as connection:
            ids = [str(row["request_id"]) for row in connection.execute("SELECT request_id FROM requests WHERE state='pending'").fetchall()]
        states = tuple(self.status(request_id) for request_id in ids)
        return tuple(state for state in states if state.state == "pending")

    def deny(self, request_id: str, *, reason: str = "user_denied") -> GrantRequestStatus:
        _text(reason, "grant request denial reason", 256)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request_id)
            request = _request_from_json(str(row["payload"]))
            if row["state"] != "pending":
                connection.commit()
                return self._status_from_row(row)
            if float(self.clock()) >= request.request_expires_at:
                connection.execute(
                    "UPDATE requests SET state='expired', decision_reason='approval_timeout', decided_at=? WHERE request_id=?",
                    (float(self.clock()), request_id),
                )
            else:
                connection.execute(
                    "UPDATE requests SET state='denied', decision_reason=?, decided_at=? WHERE request_id=?",
                    (reason, float(self.clock()), request_id),
                )
            connection.commit()
        return self.status(request_id)

    def approve(
        self,
        request_id: str,
        *,
        approval: LocalGrantApproval,
        limits: Optional[ApprovalLimits] = None,
    ) -> GrantRequestStatus:
        """Approve locally; never expose this method as a hosted model tool."""

        limits = limits or ApprovalLimits()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(connection, request_id)
                request = _request_from_json(str(row["payload"]))
                if row["state"] == "approved":
                    connection.commit()
                    return self._status_from_row(row)
                if row["state"] != "pending":
                    raise GrantRequestDenied(f"cannot approve request in state {row['state']}")
                now = float(self.clock())
                if now >= request.request_expires_at:
                    connection.execute(
                        "UPDATE requests SET state='expired', decision_reason='approval_timeout', decided_at=? WHERE request_id=?",
                        (now, request_id),
                    )
                    connection.commit()
                    raise GrantRequestDenied("grant request expired before local approval")
                values = self._approved_values(request, limits)
                grant_id = _grant_id_for_request(request.request_id)
                grant = ModelGrant(
                    grant_id=grant_id,
                    scope=request.scope,
                    selectors=request.selectors,
                    free_only=request.free_only,
                    max_cost_microusd=values["max_cost_microusd"],
                    max_calls=values["max_calls"],
                    max_input_tokens=values["max_input_tokens"],
                    max_output_tokens=values["max_output_tokens"],
                    max_request_output_tokens=values["max_request_output_tokens"],
                    max_parallel=values["max_parallel"],
                    allow_fallback=request.allow_fallback,
                    issued_at=now,
                    expires_at=now + values["grant_ttl_seconds"],
                    max_price_age_seconds=request.max_price_age_seconds,
                    approval=approval,
                    label=request.label,
                )
                # Keep the request transaction locked while authority is minted,
                # preventing a concurrent deny/second approval from racing it.
                self.grant_store.issue(grant)
                connection.execute(
                    "UPDATE requests SET state='approved', grant_id=?, decision_reason='user_approved', decided_at=? WHERE request_id=?",
                    (grant_id, now, request_id),
                )
                connection.commit()
                return self.status(request_id)
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _approved_values(request: ModelGrantRequest, limits: ApprovalLimits) -> dict[str, int]:
        values: dict[str, int] = {}
        for name in (
            "max_cost_microusd",
            "max_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_request_output_tokens",
            "max_parallel",
            "grant_ttl_seconds",
        ):
            requested = int(getattr(request, name))
            local = getattr(limits, name)
            chosen = requested if local is None else int(local)
            if chosen > requested:
                raise GrantRequestDenied(f"local approval cannot widen requested {name}")
            values[name] = chosen
        if min(
            values["max_calls"],
            values["max_input_tokens"],
            values["max_output_tokens"],
            values["max_request_output_tokens"],
            values["max_parallel"],
            values["grant_ttl_seconds"],
        ) < 1:
            raise GrantRequestDenied("local approval cannot reduce a required budget to zero")
        if values["max_request_output_tokens"] > values["max_output_tokens"]:
            raise GrantRequestDenied("approved per-call output exceeds approved total output")
        if request.free_only and values["max_cost_microusd"] != 0:
            raise GrantRequestDenied("free-only approval cannot create a money budget")
        return values

    @staticmethod
    def _row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise GrantRequestError("grant request does not exist")
        return row

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> GrantRequestStatus:
        return GrantRequestStatus(
            request=_request_from_json(str(row["payload"])),
            state=str(row["state"]),
            grant_id=str(row["grant_id"]) if row["grant_id"] is not None else None,
            decision_reason=(
                str(row["decision_reason"])
                if row["decision_reason"] is not None
                else None
            ),
        )


def new_request_id() -> str:
    return f"grant-request-{hashlib.sha256(f'{time.time_ns()}'.encode()).hexdigest()[:32]}"


def free_helper_request(
    *,
    requester_id: str,
    scope: GrantScope,
    selectors: tuple[ModelSelector, ...],
    created_at: Optional[float] = None,
    label: str = "Free AI helpers",
) -> ModelGrantRequest:
    """Conservative product default for a one-click free-model grant request."""

    now = float(time.time() if created_at is None else created_at)
    return ModelGrantRequest(
        request_id=new_request_id(),
        requester_id=requester_id,
        scope=scope,
        selectors=selectors,
        free_only=True,
        max_cost_microusd=0,
        max_calls=20,
        max_input_tokens=2_000_000,
        max_output_tokens=200_000,
        max_request_output_tokens=32_000,
        max_parallel=2,
        allow_fallback=True,
        grant_ttl_seconds=2 * 60 * 60,
        request_expires_at=now + 10 * 60,
        max_price_age_seconds=5 * 60,
        created_at=now,
        label=label,
    )


def _grant_id_for_request(request_id: str) -> str:
    return "grant-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def _request_json(request: ModelGrantRequest) -> str:
    return json.dumps(
        {
            "request_id": request.request_id,
            "requester_id": request.requester_id,
            "scope": dataclasses.asdict(request.scope),
            "selectors": [dataclasses.asdict(item) for item in request.selectors],
            "free_only": request.free_only,
            "max_cost_microusd": request.max_cost_microusd,
            "max_calls": request.max_calls,
            "max_input_tokens": request.max_input_tokens,
            "max_output_tokens": request.max_output_tokens,
            "max_request_output_tokens": request.max_request_output_tokens,
            "max_parallel": request.max_parallel,
            "allow_fallback": request.allow_fallback,
            "grant_ttl_seconds": request.grant_ttl_seconds,
            "request_expires_at": request.request_expires_at,
            "max_price_age_seconds": request.max_price_age_seconds,
            "created_at": request.created_at,
            "label": request.label,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _request_from_json(raw: str) -> ModelGrantRequest:
    value = json.loads(raw)
    return ModelGrantRequest(
        request_id=value["request_id"],
        requester_id=value["requester_id"],
        scope=GrantScope(**value["scope"]),
        selectors=tuple(ModelSelector(**item) for item in value["selectors"]),
        free_only=value["free_only"],
        max_cost_microusd=value["max_cost_microusd"],
        max_calls=value["max_calls"],
        max_input_tokens=value["max_input_tokens"],
        max_output_tokens=value["max_output_tokens"],
        max_request_output_tokens=value["max_request_output_tokens"],
        max_parallel=value["max_parallel"],
        allow_fallback=value["allow_fallback"],
        grant_ttl_seconds=value["grant_ttl_seconds"],
        request_expires_at=value["request_expires_at"],
        max_price_age_seconds=value["max_price_age_seconds"],
        created_at=value["created_at"],
        label=value.get("label", ""),
    )


def _text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must contain 1-{maximum} characters")
    if any(char in value for char in "\r\n\x00"):
        raise ValueError(f"{label} contains invalid control characters")
    return value


__all__ = [
    "ApprovalLimits",
    "GrantRequestDenied",
    "GrantRequestError",
    "GrantRequestStatus",
    "ModelGrantRequest",
    "ModelGrantRequestStore",
    "free_helper_request",
    "new_request_id",
]
