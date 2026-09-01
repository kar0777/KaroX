"""User-issued, budget-bounded authority for delegated model calls.

A hosted model is allowed to *spend* a grant but never to mint or widen one.
Grant creation belongs to a trusted local approval surface (TUI, mobile Mission
Control, or a future host channel that can cryptographically distinguish a user
message from model output).  The hosted MCP surface should expose invocation and
status/revoke-by-user workflows, never :meth:`ModelGrantStore.issue` itself.

The ledger is deliberately provider-neutral.  Provider adapters prepare a call
locally, resolve credentials locally, and attach fresh pricing evidence plus a
conservative upper bound for that request.  No API key, OAuth token, or raw
credential reference is part of this contract.

Money is stored as integer micro-USD.  Reservations are made under
``BEGIN IMMEDIATE`` before network dispatch, so two parallel agents cannot both
observe the same remaining budget and overspend it.  An ambiguous failure after
network dispatch consumes the reservation conservatively; uncertainty is never
interpreted as free.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .paths import runtime_dir

_MICRO_USD = 1_000_000
_SCHEMA_VERSION = 1
_ALLOWED_APPROVAL_CHANNELS = frozenset({"tui", "mobile", "trusted_host"})


class DelegationGrantError(RuntimeError):
    """Base error for grant persistence or authorization failures."""


class DelegationDenied(DelegationGrantError):
    """A model call is outside the authority the user issued."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class GrantScope:
    """Facts a grant may be pinned to.

    Every non-``None`` value must match at invocation time.  All ``None`` is a
    global grant and should be a deliberate local UI choice, not a default.
    """

    connection_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None

    def __post_init__(self) -> None:
        for label, value in (
            ("connection_id", self.connection_id),
            ("session_id", self.session_id),
            ("project_id", self.project_id),
        ):
            if value is None:
                continue
            _bounded_text(value, label, 256)

    def matches(self, actual: "GrantScope") -> bool:
        return all(
            expected is None or expected == observed
            for expected, observed in (
                (self.connection_id, actual.connection_id),
                (self.session_id, actual.session_id),
                (self.project_id, actual.project_id),
            )
        )


@dataclasses.dataclass(frozen=True)
class ModelSelector:
    """A provider/model glob selected by the user, not by the delegate."""

    provider: str = "*"
    model: str = "*"

    def __post_init__(self) -> None:
        _bounded_text(self.provider, "provider selector", 256)
        _bounded_text(self.model, "model selector", 512)
        if any(char in self.provider + self.model for char in "\r\n\x00"):
            raise ValueError("model selector contains invalid control characters")

    def matches(self, provider_id: str, model_id: str) -> bool:
        return fnmatch.fnmatchcase(provider_id, self.provider) and fnmatch.fnmatchcase(
            model_id, self.model
        )


@dataclasses.dataclass(frozen=True)
class BillingRate:
    """One provider-published billable dimension.

    ``unit`` is descriptive (for example ``million_input_tokens`` or
    ``request``).  The authorization layer does not guess how to multiply
    arbitrary units; the trusted provider adapter supplies the request's total
    upper bound separately.  The rates exist so a free-only grant can prove that
    *every known billable dimension* is zero.
    """

    name: str
    unit: str
    usd: float

    def __post_init__(self) -> None:
        _bounded_text(self.name, "billing rate name", 128)
        _bounded_text(self.unit, "billing rate unit", 128)
        if isinstance(self.usd, bool) or not isinstance(self.usd, (int, float)):
            raise ValueError("billing rate must be numeric")
        if not math.isfinite(float(self.usd)) or float(self.usd) < 0:
            raise ValueError("billing rate must be finite and non-negative")


@dataclasses.dataclass(frozen=True)
class PricingProof:
    """Fresh local evidence used to authorize one provider/model candidate."""

    provider_id: str
    model_id: str
    rates: tuple[BillingRate, ...]
    source: str
    observed_at: float
    authoritative: bool
    dimensions_complete: bool
    currency: str = "USD"

    def __post_init__(self) -> None:
        _bounded_text(self.provider_id, "provider id", 256)
        _bounded_text(self.model_id, "model id", 512)
        _bounded_text(self.source, "pricing source", 256)
        if not isinstance(self.observed_at, (int, float)) or not math.isfinite(
            float(self.observed_at)
        ):
            raise ValueError("pricing observation time must be finite")
        if not isinstance(self.authoritative, bool) or not isinstance(
            self.dimensions_complete, bool
        ):
            raise ValueError("pricing proof flags must be booleans")
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError("pricing currency must be a three-letter code")
        names = [item.name for item in self.rates]
        if len(names) != len(set(names)):
            raise ValueError("pricing proof contains duplicate dimensions")

    def fresh(self, *, now: float, max_age_seconds: float) -> bool:
        age = now - float(self.observed_at)
        return -5.0 <= age <= max_age_seconds

    def verified_free(self, *, now: float, max_age_seconds: float) -> bool:
        return bool(
            self.authoritative
            and self.dimensions_complete
            and self.rates
            and self.fresh(now=now, max_age_seconds=max_age_seconds)
            and all(float(item.usd) == 0.0 for item in self.rates)
        )

    def usable_for_bounded_spend(self, *, now: float, max_age_seconds: float) -> bool:
        return bool(
            self.authoritative
            and self.dimensions_complete
            and self.rates
            and self.fresh(now=now, max_age_seconds=max_age_seconds)
        )

    def fingerprint(self) -> str:
        raw = json.dumps(
            {
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "rates": [dataclasses.asdict(item) for item in self.rates],
                "source": self.source,
                "observed_at": round(float(self.observed_at), 3),
                "authoritative": self.authoritative,
                "dimensions_complete": self.dimensions_complete,
                "currency": self.currency,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class LocalGrantApproval:
    """Content-free proof that a trusted local surface observed user approval."""

    approval_id: str
    channel: str
    approved_at: float
    approval_text_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        _bounded_text(self.approval_id, "approval id", 256)
        if self.channel not in _ALLOWED_APPROVAL_CHANNELS:
            raise ValueError("unsupported grant approval channel")
        if not isinstance(self.approved_at, (int, float)) or not math.isfinite(
            float(self.approved_at)
        ):
            raise ValueError("approval time must be finite")
        if self.approval_text_sha256 is not None:
            value = self.approval_text_sha256
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
                raise ValueError("approval text hash must be SHA-256 hex")


@dataclasses.dataclass(frozen=True)
class ModelGrant:
    grant_id: str
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
    issued_at: float
    expires_at: float
    max_price_age_seconds: float
    approval: LocalGrantApproval
    label: str = ""

    def __post_init__(self) -> None:
        _bounded_text(self.grant_id, "grant id", 256)
        if not self.selectors:
            raise ValueError("grant requires at least one model selector")
        if not isinstance(self.free_only, bool) or not isinstance(self.allow_fallback, bool):
            raise ValueError("grant flags must be booleans")
        for label, value in (
            ("max_cost_microusd", self.max_cost_microusd),
            ("max_calls", self.max_calls),
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_request_output_tokens", self.max_request_output_tokens),
            ("max_parallel", self.max_parallel),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.max_calls < 1 or self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("grant call and token budgets must be positive")
        if self.max_request_output_tokens < 1 or self.max_parallel < 1:
            raise ValueError("per-request output and parallel budgets must be positive")
        if self.max_request_output_tokens > self.max_output_tokens:
            raise ValueError("per-request output cap exceeds total output budget")
        if self.free_only and self.max_cost_microusd != 0:
            raise ValueError("free-only grants must have a zero money budget")
        if not isinstance(self.issued_at, (int, float)) or not isinstance(
            self.expires_at, (int, float)
        ):
            raise ValueError("grant timestamps must be numeric")
        if not float(self.expires_at) > float(self.issued_at):
            raise ValueError("grant expiry must be after issue time")
        if (
            not isinstance(self.max_price_age_seconds, (int, float))
            or not 1.0 <= float(self.max_price_age_seconds) <= 86_400.0
        ):
            raise ValueError("pricing proof age must be between 1 second and 24 hours")
        if self.label:
            _bounded_text(self.label, "grant label", 256)

    @property
    def max_cost_usd(self) -> float:
        return self.max_cost_microusd / _MICRO_USD


@dataclasses.dataclass(frozen=True)
class GrantUsage:
    grant: ModelGrant
    revoked: bool
    revoke_reason: Optional[str]
    used_calls: int
    used_input_tokens: int
    used_output_tokens: int
    spent_microusd: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_microusd: int
    active_reservations: int

    @property
    def remaining_calls(self) -> int:
        return max(0, self.grant.max_calls - self.used_calls)

    @property
    def remaining_cost_microusd(self) -> int:
        return max(
            0,
            self.grant.max_cost_microusd
            - self.spent_microusd
            - self.reserved_microusd,
        )


@dataclasses.dataclass(frozen=True)
class ModelReservation:
    reservation_id: str
    grant_id: str
    idempotency_key: str
    provider_id: str
    model_id: str
    input_token_upper_bound: int
    output_token_upper_bound: int
    cost_upper_bound_microusd: int
    pricing_fingerprint: str
    state: str
    network_started: bool


@dataclasses.dataclass(frozen=True)
class Settlement:
    reservation_id: str
    spent_microusd: int
    input_tokens: int
    output_tokens: int
    cost_evidence: str
    breached: bool
    breach_reason: Optional[str]


@dataclasses.dataclass(frozen=True)
class PreparedModelCall:
    """A credential-free call prepared locally by a trusted provider adapter."""

    provider_id: str
    model_id: str
    input_token_upper_bound: int
    output_token_upper_bound: int
    cost_upper_bound_microusd: int
    pricing: PricingProof
    payload: Any

    def __post_init__(self) -> None:
        for label, value in (
            ("input token upper bound", self.input_token_upper_bound),
            ("output token upper bound", self.output_token_upper_bound),
            ("cost upper bound", self.cost_upper_bound_microusd),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.output_token_upper_bound < 1:
            raise ValueError("output token upper bound must be positive")
        if self.pricing.provider_id != self.provider_id or self.pricing.model_id != self.model_id:
            raise ValueError("pricing proof does not describe the prepared model")


@dataclasses.dataclass(frozen=True)
class ModelExecutionResult:
    payload: Any
    input_tokens: int
    output_tokens: int
    actual_cost_microusd: Optional[int] = None
    cost_evidence: str = "unavailable"


class LocalModelExecutor(Protocol):
    def execute(self, call: PreparedModelCall) -> ModelExecutionResult: ...


class ModelGrantStore:
    """Cross-process SQLite ledger for grants and spend reservations."""

    def __init__(self, path: Optional[Path] = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(
            path or (runtime_dir() / "vnext" / "model-delegation-grants.sqlite3")
        ).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version is None or version["value"] != str(_SCHEMA_VERSION):
                raise DelegationGrantError("model grant ledger has unsupported schema")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revoked_at REAL,
                    revoke_reason TEXT,
                    used_calls INTEGER NOT NULL DEFAULT 0,
                    used_input_tokens INTEGER NOT NULL DEFAULT 0,
                    used_output_tokens INTEGER NOT NULL DEFAULT 0,
                    spent_microusd INTEGER NOT NULL DEFAULT 0,
                    reserved_input_tokens INTEGER NOT NULL DEFAULT 0,
                    reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
                    reserved_microusd INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
                    idempotency_key TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    input_upper INTEGER NOT NULL,
                    output_upper INTEGER NOT NULL,
                    cost_upper INTEGER NOT NULL,
                    pricing_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    network_started INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    settled_at REAL,
                    actual_input INTEGER,
                    actual_output INTEGER,
                    actual_cost INTEGER,
                    cost_evidence TEXT,
                    UNIQUE(grant_id, idempotency_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_grant_res_active "
                "ON reservations(grant_id, state)"
            )

    def issue(self, grant: ModelGrant) -> GrantUsage:
        """Persist a grant created by a trusted *local* approval surface.

        Do not expose this method as an MCP tool.  Hosted models may request an
        approval UX, but only the local UX should construct ``LocalGrantApproval``
        and call ``issue`` after a human action.
        """

        if not isinstance(grant, ModelGrant):
            raise ValueError("issue requires a ModelGrant")
        payload = _grant_json(grant)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload FROM grants WHERE grant_id=?",
                    (grant.grant_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["payload"]) != payload:
                        raise DelegationGrantError(
                            "grant id already exists with different authority"
                        )
                    connection.commit()
                    return self.status(grant.grant_id)
                connection.execute(
                    "INSERT INTO grants(grant_id, payload) VALUES (?, ?)",
                    (grant.grant_id, payload),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.status(grant.grant_id)

    def revoke(self, grant_id: str, *, reason: str = "user_revoked") -> GrantUsage:
        _bounded_text(grant_id, "grant id", 256)
        _bounded_text(reason, "revoke reason", 256)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._grant_row(connection, grant_id)
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE grants SET revoked_at=?, revoke_reason=? WHERE grant_id=?",
                    (float(self.clock()), reason, grant_id),
                )
            connection.commit()
        return self.status(grant_id)

    def status(self, grant_id: str) -> GrantUsage:
        _bounded_text(grant_id, "grant id", 256)
        with self._connect() as connection:
            row = self._grant_row(connection, grant_id)
            active = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM reservations WHERE grant_id=? AND state='reserved'",
                    (grant_id,),
                ).fetchone()["n"]
            )
            return _usage_from_row(row, active)

    def list(self) -> tuple[GrantUsage, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM grants ORDER BY rowid DESC").fetchall()
            active_rows = connection.execute(
                "SELECT grant_id, COUNT(*) AS n FROM reservations "
                "WHERE state='reserved' GROUP BY grant_id"
            ).fetchall()
            active = {str(row["grant_id"]): int(row["n"]) for row in active_rows}
            return tuple(_usage_from_row(row, active.get(str(row["grant_id"]), 0)) for row in rows)

    def reserve(
        self,
        grant_id: str,
        *,
        scope: GrantScope,
        call: PreparedModelCall,
        idempotency_key: str,
    ) -> ModelReservation:
        _bounded_text(idempotency_key, "idempotency key", 256)
        now = float(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._grant_row(connection, grant_id)
                grant = _grant_from_json(str(row["payload"]))
                existing = connection.execute(
                    "SELECT * FROM reservations WHERE grant_id=? AND idempotency_key=?",
                    (grant_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    replay = _reservation_from_row(existing)
                    if (
                        replay.provider_id != call.provider_id
                        or replay.model_id != call.model_id
                        or replay.input_token_upper_bound != call.input_token_upper_bound
                        or replay.output_token_upper_bound != call.output_token_upper_bound
                        or replay.cost_upper_bound_microusd != call.cost_upper_bound_microusd
                        or replay.pricing_fingerprint != call.pricing.fingerprint()
                    ):
                        raise DelegationDenied(
                            "idempotency_conflict",
                            "idempotency key was already used for a different model call",
                        )
                    connection.commit()
                    return replay
                self._authorize(row, grant, scope=scope, call=call, now=now, connection=connection)
                reservation_id = f"res-{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO reservations(
                        reservation_id, grant_id, idempotency_key, provider_id, model_id,
                        input_upper, output_upper, cost_upper, pricing_fingerprint,
                        state, network_started, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 0, ?)
                    """,
                    (
                        reservation_id,
                        grant_id,
                        idempotency_key,
                        call.provider_id,
                        call.model_id,
                        call.input_token_upper_bound,
                        call.output_token_upper_bound,
                        call.cost_upper_bound_microusd,
                        call.pricing.fingerprint(),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE grants SET
                        used_calls=used_calls+1,
                        reserved_input_tokens=reserved_input_tokens+?,
                        reserved_output_tokens=reserved_output_tokens+?,
                        reserved_microusd=reserved_microusd+?
                    WHERE grant_id=?
                    """,
                    (
                        call.input_token_upper_bound,
                        call.output_token_upper_bound,
                        call.cost_upper_bound_microusd,
                        grant_id,
                    ),
                )
                connection.commit()
                return ModelReservation(
                    reservation_id=reservation_id,
                    grant_id=grant_id,
                    idempotency_key=idempotency_key,
                    provider_id=call.provider_id,
                    model_id=call.model_id,
                    input_token_upper_bound=call.input_token_upper_bound,
                    output_token_upper_bound=call.output_token_upper_bound,
                    cost_upper_bound_microusd=call.cost_upper_bound_microusd,
                    pricing_fingerprint=call.pricing.fingerprint(),
                    state="reserved",
                    network_started=False,
                )
            except Exception:
                connection.rollback()
                raise

    def _authorize(
        self,
        row: sqlite3.Row,
        grant: ModelGrant,
        *,
        scope: GrantScope,
        call: PreparedModelCall,
        now: float,
        connection: sqlite3.Connection,
    ) -> None:
        if row["revoked_at"] is not None:
            raise DelegationDenied("revoked", "model delegation grant has been revoked")
        if now >= grant.expires_at:
            raise DelegationDenied("expired", "model delegation grant has expired")
        if not grant.scope.matches(scope):
            raise DelegationDenied("scope_mismatch", "model delegation grant does not cover this session/project")
        if not any(item.matches(call.provider_id, call.model_id) for item in grant.selectors):
            raise DelegationDenied("model_not_allowed", "provider/model is not allowed by this grant")
        proof = call.pricing
        if proof.provider_id != call.provider_id or proof.model_id != call.model_id:
            raise DelegationDenied("pricing_mismatch", "pricing proof belongs to a different model")
        if grant.free_only:
            if call.cost_upper_bound_microusd != 0:
                raise DelegationDenied("not_free", "free-only grant refuses a non-zero request cost")
            if not proof.verified_free(now=now, max_age_seconds=grant.max_price_age_seconds):
                raise DelegationDenied(
                    "free_price_unproven",
                    "model is not proven free by fresh complete provider pricing",
                )
        elif proof.currency.upper() != "USD":
            raise DelegationDenied(
                "currency_unproven",
                "paid delegation requires a USD-normalized price proof",
            )
        elif not proof.usable_for_bounded_spend(
            now=now, max_age_seconds=grant.max_price_age_seconds
        ):
            raise DelegationDenied(
                "price_unproven",
                "paid delegation requires fresh complete authoritative pricing",
            )
        if call.output_token_upper_bound > grant.max_request_output_tokens:
            raise DelegationDenied("request_output_cap", "model call exceeds the per-request output cap")
        used_calls = int(row["used_calls"])
        if used_calls + 1 > grant.max_calls:
            raise DelegationDenied("call_budget", "model delegation call budget is exhausted")
        active = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM reservations WHERE grant_id=? AND state='reserved'",
                (grant.grant_id,),
            ).fetchone()["n"]
        )
        if active >= grant.max_parallel:
            raise DelegationDenied("parallel_budget", "model delegation parallel-call budget is exhausted")
        if (
            int(row["used_input_tokens"])
            + int(row["reserved_input_tokens"])
            + call.input_token_upper_bound
            > grant.max_input_tokens
        ):
            raise DelegationDenied("input_token_budget", "model delegation input-token budget is exhausted")
        if (
            int(row["used_output_tokens"])
            + int(row["reserved_output_tokens"])
            + call.output_token_upper_bound
            > grant.max_output_tokens
        ):
            raise DelegationDenied("output_token_budget", "model delegation output-token budget is exhausted")
        if (
            int(row["spent_microusd"])
            + int(row["reserved_microusd"])
            + call.cost_upper_bound_microusd
            > grant.max_cost_microusd
        ):
            raise DelegationDenied("money_budget", "model delegation money budget is exhausted")

    def mark_network_started(self, reservation_id: str) -> ModelReservation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._reservation_row(connection, reservation_id)
            if row["state"] != "reserved":
                connection.commit()
                return _reservation_from_row(row)
            connection.execute(
                "UPDATE reservations SET network_started=1 WHERE reservation_id=?",
                (reservation_id,),
            )
            connection.commit()
        return self.reservation(reservation_id)

    def reservation(self, reservation_id: str) -> ModelReservation:
        with self._connect() as connection:
            return _reservation_from_row(self._reservation_row(connection, reservation_id))

    def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        actual_cost_microusd: Optional[int],
        cost_evidence: str,
    ) -> Settlement:
        input_tokens = _non_negative_int(input_tokens, "input tokens")
        output_tokens = _non_negative_int(output_tokens, "output tokens")
        _bounded_text(cost_evidence, "cost evidence", 128)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._reservation_row(connection, reservation_id)
                if row["state"] == "settled":
                    connection.commit()
                    return _settlement_from_row(row)
                if row["state"] != "reserved":
                    raise DelegationGrantError("only a reserved model call can be settled")
                grant_row = self._grant_row(connection, str(row["grant_id"]))
                grant = _grant_from_json(str(grant_row["payload"]))
                reserved_cost = int(row["cost_upper"])
                if actual_cost_microusd is None:
                    actual_cost = reserved_cost if bool(row["network_started"]) else 0
                    evidence = "reserved_upper_bound" if bool(row["network_started"]) else "not_dispatched"
                else:
                    actual_cost = _non_negative_int(actual_cost_microusd, "actual model cost")
                    evidence = cost_evidence
                breach_reason: Optional[str] = None
                if input_tokens > int(row["input_upper"]):
                    breach_reason = "input_usage_exceeded_reservation"
                elif output_tokens > int(row["output_upper"]):
                    breach_reason = "output_usage_exceeded_reservation"
                elif actual_cost > reserved_cost:
                    breach_reason = "cost_exceeded_reservation"
                elif grant.free_only and actual_cost > 0:
                    breach_reason = "free_model_reported_cost"
                new_input = int(grant_row["used_input_tokens"]) + input_tokens
                new_output = int(grant_row["used_output_tokens"]) + output_tokens
                new_cost = int(grant_row["spent_microusd"]) + actual_cost
                if breach_reason is None and new_input > grant.max_input_tokens:
                    breach_reason = "input_budget_breached"
                if breach_reason is None and new_output > grant.max_output_tokens:
                    breach_reason = "output_budget_breached"
                if breach_reason is None and new_cost > grant.max_cost_microusd:
                    breach_reason = "money_budget_breached"
                connection.execute(
                    """
                    UPDATE grants SET
                        used_input_tokens=?, used_output_tokens=?, spent_microusd=?,
                        reserved_input_tokens=MAX(0, reserved_input_tokens-?),
                        reserved_output_tokens=MAX(0, reserved_output_tokens-?),
                        reserved_microusd=MAX(0, reserved_microusd-?),
                        revoked_at=CASE WHEN ? IS NULL THEN revoked_at ELSE ? END,
                        revoke_reason=CASE WHEN ? IS NULL THEN revoke_reason ELSE ? END
                    WHERE grant_id=?
                    """,
                    (
                        new_input,
                        new_output,
                        new_cost,
                        int(row["input_upper"]),
                        int(row["output_upper"]),
                        reserved_cost,
                        breach_reason,
                        float(self.clock()),
                        breach_reason,
                        breach_reason,
                        grant.grant_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE reservations SET
                        state='settled', settled_at=?, actual_input=?, actual_output=?,
                        actual_cost=?, cost_evidence=?
                    WHERE reservation_id=?
                    """,
                    (
                        float(self.clock()),
                        input_tokens,
                        output_tokens,
                        actual_cost,
                        evidence,
                        reservation_id,
                    ),
                )
                connection.commit()
                return Settlement(
                    reservation_id=reservation_id,
                    spent_microusd=actual_cost,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_evidence=evidence,
                    breached=breach_reason is not None,
                    breach_reason=breach_reason,
                )
            except Exception:
                connection.rollback()
                raise

    def abort(self, reservation_id: str, *, reason: str = "execution_failed") -> Settlement:
        """Release a pre-dispatch reservation or conservatively consume an ambiguous one."""

        _bounded_text(reason, "abort reason", 128)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._reservation_row(connection, reservation_id)
                if row["state"] == "settled":
                    connection.commit()
                    return _settlement_from_row(row)
                if row["state"] == "cancelled":
                    connection.commit()
                    return Settlement(reservation_id, 0, 0, 0, reason, False, None)
                if bool(row["network_started"]):
                    # Commit this transaction before the public settle opens its own.
                    connection.commit()
                    return self.settle(
                        reservation_id,
                        input_tokens=int(row["input_upper"]),
                        output_tokens=int(row["output_upper"]),
                        actual_cost_microusd=None,
                        cost_evidence="ambiguous_after_dispatch",
                    )
                connection.execute(
                    """
                    UPDATE grants SET
                        reserved_input_tokens=MAX(0, reserved_input_tokens-?),
                        reserved_output_tokens=MAX(0, reserved_output_tokens-?),
                        reserved_microusd=MAX(0, reserved_microusd-?)
                    WHERE grant_id=?
                    """,
                    (
                        int(row["input_upper"]),
                        int(row["output_upper"]),
                        int(row["cost_upper"]),
                        str(row["grant_id"]),
                    ),
                )
                connection.execute(
                    "UPDATE reservations SET state='cancelled', settled_at=?, cost_evidence=? "
                    "WHERE reservation_id=?",
                    (float(self.clock()), reason, reservation_id),
                )
                connection.commit()
                # Calls are intentionally not refunded: a delegate cannot loop on
                # repeated local failures forever just because no provider request left.
                return Settlement(reservation_id, 0, 0, 0, reason, False, None)
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _grant_row(connection: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise DelegationDenied("unknown_grant", "model delegation grant does not exist")
        return row

    @staticmethod
    def _reservation_row(connection: sqlite3.Connection, reservation_id: str) -> sqlite3.Row:
        _bounded_text(reservation_id, "reservation id", 256)
        row = connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise DelegationGrantError("model reservation does not exist")
        return row


class GrantBoundDelegator:
    """Wrap a local provider executor with grant reservation/settlement."""

    def __init__(self, store: ModelGrantStore, executor: LocalModelExecutor) -> None:
        self.store = store
        self.executor = executor

    def invoke(
        self,
        grant_id: str,
        *,
        scope: GrantScope,
        call: PreparedModelCall,
        idempotency_key: str,
    ) -> tuple[ModelExecutionResult, Settlement]:
        reservation = self.store.reserve(
            grant_id,
            scope=scope,
            call=call,
            idempotency_key=idempotency_key,
        )
        if reservation.state == "settled":
            raise DelegationDenied(
                "already_settled",
                "idempotent model call already settled; use the persisted result layer",
            )
        if reservation.network_started:
            raise DelegationDenied(
                "in_progress",
                "idempotent model call is already in progress",
            )
        self.store.mark_network_started(reservation.reservation_id)
        try:
            result = self.executor.execute(call)
        except Exception:
            self.store.abort(reservation.reservation_id)
            raise
        settlement = self.store.settle(
            reservation.reservation_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            actual_cost_microusd=result.actual_cost_microusd,
            cost_evidence=result.cost_evidence,
        )
        return result, settlement


def usd_to_microusd(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("USD amount must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError("USD amount must be finite and non-negative")
    # Ceiling is deliberate: a hard budget must never round a positive estimate down.
    return int(math.ceil(float(value) * _MICRO_USD - 1e-12))


def new_grant_id() -> str:
    return f"grant-{uuid.uuid4().hex}"


def hash_approval_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("approval text must be text")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _grant_json(grant: ModelGrant) -> str:
    return json.dumps(
        {
            "grant_id": grant.grant_id,
            "scope": dataclasses.asdict(grant.scope),
            "selectors": [dataclasses.asdict(item) for item in grant.selectors],
            "free_only": grant.free_only,
            "max_cost_microusd": grant.max_cost_microusd,
            "max_calls": grant.max_calls,
            "max_input_tokens": grant.max_input_tokens,
            "max_output_tokens": grant.max_output_tokens,
            "max_request_output_tokens": grant.max_request_output_tokens,
            "max_parallel": grant.max_parallel,
            "allow_fallback": grant.allow_fallback,
            "issued_at": grant.issued_at,
            "expires_at": grant.expires_at,
            "max_price_age_seconds": grant.max_price_age_seconds,
            "approval": dataclasses.asdict(grant.approval),
            "label": grant.label,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _grant_from_json(raw: str) -> ModelGrant:
    value = json.loads(raw)
    return ModelGrant(
        grant_id=value["grant_id"],
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
        issued_at=value["issued_at"],
        expires_at=value["expires_at"],
        max_price_age_seconds=value["max_price_age_seconds"],
        approval=LocalGrantApproval(**value["approval"]),
        label=value.get("label", ""),
    )


def _usage_from_row(row: sqlite3.Row, active: int) -> GrantUsage:
    return GrantUsage(
        grant=_grant_from_json(str(row["payload"])),
        revoked=row["revoked_at"] is not None,
        revoke_reason=str(row["revoke_reason"]) if row["revoke_reason"] is not None else None,
        used_calls=int(row["used_calls"]),
        used_input_tokens=int(row["used_input_tokens"]),
        used_output_tokens=int(row["used_output_tokens"]),
        spent_microusd=int(row["spent_microusd"]),
        reserved_input_tokens=int(row["reserved_input_tokens"]),
        reserved_output_tokens=int(row["reserved_output_tokens"]),
        reserved_microusd=int(row["reserved_microusd"]),
        active_reservations=int(active),
    )


def _reservation_from_row(row: sqlite3.Row) -> ModelReservation:
    return ModelReservation(
        reservation_id=str(row["reservation_id"]),
        grant_id=str(row["grant_id"]),
        idempotency_key=str(row["idempotency_key"]),
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        input_token_upper_bound=int(row["input_upper"]),
        output_token_upper_bound=int(row["output_upper"]),
        cost_upper_bound_microusd=int(row["cost_upper"]),
        pricing_fingerprint=str(row["pricing_fingerprint"]),
        state=str(row["state"]),
        network_started=bool(row["network_started"]),
    )


def _settlement_from_row(row: sqlite3.Row) -> Settlement:
    return Settlement(
        reservation_id=str(row["reservation_id"]),
        spent_microusd=int(row["actual_cost"] or 0),
        input_tokens=int(row["actual_input"] or 0),
        output_tokens=int(row["actual_output"] or 0),
        cost_evidence=str(row["cost_evidence"] or "unknown"),
        breached=False,
        breach_reason=None,
    )


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must contain 1-{maximum} characters")
    if any(char in value for char in "\r\n\x00"):
        raise ValueError(f"{label} contains invalid control characters")
    return value


def _non_negative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


__all__ = [
    "BillingRate",
    "DelegationDenied",
    "DelegationGrantError",
    "GrantBoundDelegator",
    "GrantScope",
    "GrantUsage",
    "LocalGrantApproval",
    "LocalModelExecutor",
    "ModelExecutionResult",
    "ModelGrant",
    "ModelGrantStore",
    "ModelReservation",
    "ModelSelector",
    "PreparedModelCall",
    "PricingProof",
    "Settlement",
    "hash_approval_text",
    "new_grant_id",
    "usd_to_microusd",
]
