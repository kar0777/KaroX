"""Typed view models every UI reads instead of parsing log text.

The event bus gave the product one ordered, bounded, redacted stream. It did not
give a UI anything to draw: a stream of eleven event families is not a session
list, and a terminal that re-folds the whole history on every tick is the reason
the TUI kept polling ``session.json`` and parsing ``provider_history`` instead.

This module is that missing layer, and it is deliberately the *only* one:

* :class:`SessionViewStore` folds :class:`~karox.event_bus.Event` values into
  per-session state. The Session Browser, the detail view, the Smart Stop
  decision screen, a ``--json`` CLI and a support export all read the same fold,
  so two surfaces cannot disagree about what a session is doing.
* the fold is **incremental**. A UI calls :meth:`SessionViewStore.sync` with the
  bus and only events after the stored cursor are folded, which is what
  ``since_seq`` on the bus exists for. Nothing is re-parsed and nothing is
  re-rendered wholesale.
* the fold is **bounded**. Timeline entries, tool calls and tracked sessions all
  have hard caps, and everything discarded is counted and reported rather than
  silently lost.

Three rules that are easy to get wrong, and are enforced here
------------------------------------------------------------

**No second redaction.** Payloads are redacted once, by the bus, on the way in.
This layer does not scrub anything -- scrubbing twice is how a subtly different
second implementation ends up being the one with the hole. Instead it copies
*only* an explicit per-kind allowlist of keys into a timeline entry. An unknown
key is dropped because it was never asked for, so a payload field added later by
any publisher cannot reach a screen by accident, and a confirmation token has no
path here even if one were somehow published.

**No user-facing text.** Every field this module produces is either a number, a
timestamp or a stable machine identifier: ``waiting_reason`` is a code such as
``confirmation_required``, ``primary_action`` is an action id such as
``review_risk``. Translation belongs to the message catalogs, and a view model
that carried Russian or English prose could not be rendered in the other
language or asserted on in a test.

**A UI bug must not stop an agent.** :meth:`SessionViewStore.apply` never raises
on a malformed or hostile payload: every field goes through a coercion helper and
an unexpected shape is ignored. The bus already isolates a raising subscriber,
but a view layer that relies on that isolation would silently stop updating.

Event payload conventions
-------------------------

The bus deliberately carries a free-form ``data`` mapping, so the conventions the
fold understands are written down here and are all optional. A publisher that
supplies none of them still produces a usable row: the summary, level, source and
sequence alone give the Session Browser a last-activity line.

``SESSION_STATE``
    ``status``, ``phase``, ``current_step``, ``waiting_reason``, ``task``,
    ``agent``, ``provider``, ``model``, ``workspace_mode``, ``access_profile``,
    ``changed_files`` (a count or a list), ``git``, ``diff``.
``TOOL_CALL``
    ``call_id``, ``tool``, ``phase`` (``started``/``finished``), ``ok``,
    ``duration_ms``, ``detail``.
``AGENT_ACTION``
    ``step``, ``usage`` (``input_tokens``/``output_tokens``/``total_tokens``, each
    an ``int``), ``cost`` (currency to amount), and ``budgets``, a mapping of
    budget name to ``{"used", "limit", "unit"}``, where the two names are
    ``usage`` for the token allowance and ``cost`` for the money allowance. Those
    exact names matter; see the naming constraint below.
``BROWSER_ACTION``
    ``action``, ``tab_id``, ``url``, ``origin``, ``navigation_generation``,
    ``takeover``.
``RISK_DECISION``
    ``allowed``, ``reason``, ``risk``, ``reasons``, ``action_digest``,
    ``preview`` -- exactly what :mod:`karox.core` already publishes.
``CONFIRMATION``
    ``action_digest``, ``decision`` (``requested``/``approved``/``rejected``/
    ``expired``). Never a token: the ledger keeps tokens out of events by
    construction, and this layer has no key for one.
``PERFORMANCE_SPAN``
    ``span``, ``component``, ``duration_ms``, ``failed``.
``EVIDENCE``
    ``evidence_id``, ``kind``, ``summary``.
``ERROR``
    ``code``, ``message``.
``CONNECTION_STATE`` / ``HEALTH_CHANGE``
    ``connection``/``component``, ``status``.

A naming constraint every publisher shares
------------------------------------------

:func:`karox.security.redact` blanks any payload key whose name contains
``token``, ``secret``, ``key``, ``password``, ``credential``, ``cookie`` or
``authorization``. It runs on the way into the bus, before this layer sees
anything, and it is right to be that blunt: a key called ``session_token`` must
never survive whatever a caller believed it held.

The consequence is that a *budget* cannot be published under a name like
``token_budget``, and not under ``budgets.tokens`` either -- both contain
``token`` and both are erased before this layer is ever reached. The single
exemption in ``redact`` is a key ending in ``_tokens`` holding a non-negative
``int``, which is exactly why a token *counter* is legal as
``usage.total_tokens`` while a token *limit* is not legal under any name
containing the word.

So the two budget names are ``budgets.usage`` and ``budgets.cost``. Neither
contains a redacted substring. The unit is still reported as ``"tokens"``,
because ``redact`` blanks by *key* name and never rewrites an ordinary value.

This layer deliberately does not work around that by re-widening the redactor.
The constraint is enforced by a test, so a future publisher that invents
``token_budget`` again fails immediately instead of silently reporting a session
with no limit.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Iterable, Mapping, Optional

from .event_bus import Event, EventBus, EventKind, EventLevel

# Caps. Every one of them is a memory bound on a session that runs for hours,
# and every one reports what it discarded.
DEFAULT_TIMELINE_LIMIT = 200
DEFAULT_TOOL_CALL_LIMIT = 100
DEFAULT_SESSION_LIMIT = 200
# One UI line, matching the bus's own summary bound.
MAX_DETAIL_TEXT = 300
MAX_LIST_ITEMS = 20

# Stable action identifiers. The UI maps these to localized labels; this layer
# must not contain prose.
ACTION_REVIEW_RISK = "review_risk"
ACTION_STOP = "stop"
ACTION_RESUME = "resume"
ACTION_OPEN = "open"

# Stable waiting reasons, for the same reason.
WAIT_CONFIRMATION = "confirmation_required"

_RUNNING_STATUSES = frozenset({"running", "active", "working", "executing"})
_WAITING_STATUSES = frozenset({"waiting", "blocked", "paused", "stopped"})
_TERMINAL_STATUSES = frozenset(
    {"finished", "completed", "failed", "revoked", "cancelled"}
)

# Every status this layer can put in a view model. The UI must be able to
# translate all of them, so the localisation catalog is asserted against this
# set rather than against a hand-copied list that silently drifts.
KNOWN_STATUSES = _RUNNING_STATUSES | _WAITING_STATUSES | _TERMINAL_STATUSES

# The only payload keys that may reach a timeline entry, per event family. An
# unknown key is not scrubbed, it is simply never copied.
_TIMELINE_KEYS: Mapping[EventKind, tuple[str, ...]] = {
    EventKind.AGENT_ACTION: ("step",),
    EventKind.TOOL_CALL: ("call_id", "tool", "phase", "ok", "duration_ms"),
    EventKind.BROWSER_ACTION: (
        "action",
        "tab_id",
        "origin",
        "navigation_generation",
        "takeover",
    ),
    EventKind.SESSION_STATE: ("status", "phase", "current_step", "waiting_reason"),
    EventKind.CONNECTION_STATE: ("connection", "status"),
    EventKind.RISK_DECISION: ("allowed", "reason", "risk", "action_digest"),
    EventKind.CONFIRMATION: ("decision", "action_digest"),
    EventKind.PERFORMANCE_SPAN: ("span", "component", "duration_ms", "failed"),
    EventKind.HEALTH_CHANGE: ("component", "status"),
    EventKind.EVIDENCE: ("evidence_id", "kind"),
    EventKind.ERROR: ("code",),
}


def _text(value: Any, limit: int = MAX_DETAIL_TEXT) -> str:
    """One safe display line, with newlines removed and length bounded."""

    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "\u2026"
    return text


def _number(value: Any) -> Optional[float]:
    """A real number, or nothing. ``bool`` is not a number here."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _count(value: Any) -> Optional[int]:
    """A count from either a number or the length of a sequence."""

    number = _number(value)
    if number is not None:
        return max(int(number), 0)
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return None


def _flag(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (_text(value),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(_text(item) for item in value[:MAX_LIST_ITEMS] if _text(item))
    return ()


@dataclass(frozen=True)
class BudgetView:
    """How much of a budget a session has spent, and how much is left.

    ``limit`` is optional because an unbounded session is a real state and must
    not be shown as a full or an empty bar.
    """

    used: float = 0.0
    limit: Optional[float] = None
    unit: str = ""

    @property
    def remaining(self) -> Optional[float]:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0.0)

    @property
    def fraction(self) -> Optional[float]:
        if self.limit is None or self.limit <= 0:
            return None
        return min(self.used / self.limit, 1.0)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "limit": self.limit,
            "unit": self.unit,
            "remaining": self.remaining,
            "fraction": self.fraction,
            "exhausted": self.exhausted,
        }


@dataclass(frozen=True)
class ToolCallView:
    """One tool call, with its real duration rather than a log line."""

    call_id: str
    name: str
    started_seq: int
    started_at: float
    finished_at: Optional[float] = None
    duration_ms: Optional[float] = None
    ok: Optional[bool] = None
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "started_seq": self.started_seq,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "detail": self.detail,
            "running": self.running,
        }


@dataclass(frozen=True)
class RiskStateView:
    """The last risk verdict for a session. Never carries a token."""

    seq: int
    level: str
    allowed: bool
    reason: str = ""
    action_digest: str = ""
    reasons: tuple[str, ...] = ()
    preview: Mapping[str, Any] = field(default_factory=dict)

    @property
    def awaiting_confirmation(self) -> bool:
        return not self.allowed and self.reason == "confirmation_required"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "level": self.level,
            "allowed": self.allowed,
            "reason": self.reason,
            "action_digest": self.action_digest,
            "reasons": list(self.reasons),
            "preview": dict(self.preview),
            "awaiting_confirmation": self.awaiting_confirmation,
        }


@dataclass(frozen=True)
class TimelineEntry:
    """One row of the detail timeline: typed, ordered, bounded."""

    seq: int
    kind: str
    level: str
    source: str
    timestamp: float
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "level": self.level,
            "source": self.source,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class SessionSummary:
    """One Session Browser row.

    Every field here answers a question a person asks before choosing which
    session to open, which is why the row is this wide: a list of identifiers
    with a spinner is what made the old screen useless.
    """

    session_id: str
    title: str = ""
    agent: str = ""
    source: str = ""
    provider: str = ""
    model: str = ""
    workspace_mode: str = ""
    access_profile: str = ""
    status: str = "unknown"
    current_step: str = ""
    changed_files: int = 0
    token_budget: BudgetView = field(default_factory=BudgetView)
    cost_budget: BudgetView = field(default_factory=BudgetView)
    elapsed_seconds: float = 0.0
    waiting_reason: str = ""
    last_event_summary: str = ""
    last_event_seq: int = 0
    last_event_at: float = 0.0
    primary_action: str = ACTION_OPEN
    error_count: int = 0
    risk: Optional[RiskStateView] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "agent": self.agent,
            "source": self.source,
            "provider": self.provider,
            "model": self.model,
            "workspace_mode": self.workspace_mode,
            "access_profile": self.access_profile,
            "status": self.status,
            "current_step": self.current_step,
            "changed_files": self.changed_files,
            "token_budget": self.token_budget.to_dict(),
            "cost_budget": self.cost_budget.to_dict(),
            "elapsed_seconds": self.elapsed_seconds,
            "waiting_reason": self.waiting_reason,
            "last_event_summary": self.last_event_summary,
            "last_event_seq": self.last_event_seq,
            "last_event_at": self.last_event_at,
            "primary_action": self.primary_action,
            "error_count": self.error_count,
            "risk": self.risk.to_dict() if self.risk is not None else None,
        }


@dataclass(frozen=True)
class SessionDetail:
    """Everything the detail view shows, from typed events only."""

    summary: SessionSummary
    timeline: tuple[TimelineEntry, ...] = ()
    tool_calls: tuple[ToolCallView, ...] = ()
    browser: Mapping[str, Any] = field(default_factory=dict)
    git: Mapping[str, Any] = field(default_factory=dict)
    diff: Mapping[str, Any] = field(default_factory=dict)
    checkpoints: tuple[Mapping[str, Any], ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, float] = field(default_factory=dict)
    costs: Mapping[str, float] = field(default_factory=dict)
    errors: tuple[TimelineEntry, ...] = ()
    performance: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    pending_confirmation: Optional[RiskStateView] = None
    dropped_events: int = 0
    truncated_timeline: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "timeline": [item.to_dict() for item in self.timeline],
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "browser": dict(self.browser),
            "git": dict(self.git),
            "diff": dict(self.diff),
            "checkpoints": [dict(item) for item in self.checkpoints],
            "evidence": [dict(item) for item in self.evidence],
            "usage": dict(self.usage),
            "costs": dict(self.costs),
            "errors": [item.to_dict() for item in self.errors],
            "performance": {
                name: dict(values) for name, values in self.performance.items()
            },
            "pending_confirmation": (
                self.pending_confirmation.to_dict()
                if self.pending_confirmation is not None
                else None
            ),
            "dropped_events": self.dropped_events,
            "truncated_timeline": self.truncated_timeline,
        }


class _SessionState:
    """Mutable per-session accumulator. Snapshotted into frozen views."""

    def __init__(self, session_id: str, *, timeline_limit: int, tool_limit: int) -> None:
        self.session_id = session_id
        self.title = ""
        self.agent = ""
        self.source = ""
        self.provider = ""
        self.model = ""
        self.workspace_mode = ""
        self.access_profile = ""
        self.status = "unknown"
        self.current_step = ""
        self.waiting_reason = ""
        self.changed_files = 0
        self.token_budget = BudgetView(unit="tokens")
        self.cost_budget = BudgetView()
        self.started_at: Optional[float] = None
        self.last_event_at = 0.0
        self.last_event_seq = 0
        self.last_event_summary = ""
        self.error_count = 0
        self.risk: Optional[RiskStateView] = None
        self.pending_confirmation: Optional[RiskStateView] = None
        self.timeline: Deque[TimelineEntry] = deque(maxlen=timeline_limit)
        self.truncated_timeline = 0
        self.tool_calls: "OrderedDict[str, ToolCallView]" = OrderedDict()
        self.tool_limit = tool_limit
        self.browser: dict[str, Any] = {}
        self.git: dict[str, Any] = {}
        self.diff: dict[str, Any] = {}
        self.checkpoints: list[Mapping[str, Any]] = []
        self.evidence: list[Mapping[str, Any]] = []
        self.usage: dict[str, float] = {}
        self.costs: dict[str, float] = {}
        self.errors: Deque[TimelineEntry] = deque(maxlen=MAX_LIST_ITEMS)
        self.performance: dict[str, dict[str, float]] = {}
        self.terminal = False

    def note_tool_call(self, view: ToolCallView) -> None:
        self.tool_calls[view.call_id] = view
        self.tool_calls.move_to_end(view.call_id)
        while len(self.tool_calls) > self.tool_limit:
            self.tool_calls.popitem(last=False)


class SessionViewStore:
    """Fold the event stream into session view models, incrementally.

    Typical use from a UI::

        store = SessionViewStore()
        detach = store.attach(event_bus())   # live pushes
        store.sync(event_bus())              # backfill what already happened
        ...
        for session_id in store.consume_dirty():
            redraw(store.detail(session_id))

    ``consume_dirty`` is the debounce primitive: a burst of two hundred events
    marks one session dirty once, so a refresh timer redraws that session a
    single time instead of two hundred times.
    """

    def __init__(
        self,
        *,
        timeline_limit: int = DEFAULT_TIMELINE_LIMIT,
        tool_call_limit: int = DEFAULT_TOOL_CALL_LIMIT,
        session_limit: int = DEFAULT_SESSION_LIMIT,
        now: Callable[[], float] = time.time,
    ) -> None:
        if timeline_limit <= 0 or tool_call_limit <= 0 or session_limit <= 0:
            raise ValueError("view store limits must be positive")
        self._timeline_limit = int(timeline_limit)
        self._tool_call_limit = int(tool_call_limit)
        self._session_limit = int(session_limit)
        self._now = now
        self._sessions: "OrderedDict[str, _SessionState]" = OrderedDict()
        self._cursor = 0
        self._dirty: set[str] = set()
        self._dropped = 0
        self._evicted_sessions = 0
        self._applied = 0

    # ------------------------------------------------------------------ input

    @property
    def cursor(self) -> int:
        """The highest sequence number folded so far."""

        return self._cursor

    @property
    def dropped_events(self) -> int:
        """Events the bus evicted before this store could read them."""

        return self._dropped

    @property
    def evicted_sessions(self) -> int:
        return self._evicted_sessions

    @property
    def applied(self) -> int:
        return self._applied

    def attach(self, bus: EventBus) -> Callable[[], None]:
        """Subscribe to live events and return the detach handle."""

        def on_event(event: Event) -> None:
            # ``EventBus.subscribe`` wants ``Callable[[Event], None]`` while
            # ``apply`` returns whether the event was folded, which ``sync`` and
            # ``ingest`` need for incremental accounting. This adapter keeps the
            # bus contract without weakening that return value.
            self.apply(event)

        return bus.subscribe(on_event)

    def sync(self, bus: EventBus) -> int:
        """Fold everything after the cursor. Returns how many were folded.

        Safe to combine with :meth:`attach` in either order: the fold ignores a
        sequence number it has already seen, so an event delivered live and then
        again by a backfill is applied once.
        """

        events = bus.snapshot(since_seq=self._cursor)
        folded = 0
        for event in events:
            if self.apply(event):
                folded += 1
        self._dropped = max(self._dropped, int(bus.dropped))
        return folded

    def ingest(self, events: Iterable[Event]) -> int:
        folded = 0
        for event in events:
            if self.apply(event):
                folded += 1
        return folded

    def apply(self, event: Event) -> bool:
        """Fold one event. Never raises, whatever the payload contains.

        Returns whether the event was folded; a replay of an already-folded
        sequence number returns ``False``.
        """

        try:
            return self._apply(event)
        except Exception:
            # A view layer that can throw is a view layer that stops updating.
            # Nothing in a payload is worth that, so a malformed event is
            # ignored rather than allowed to break the fold.
            return False

    def _apply(self, event: Event) -> bool:
        seq = int(getattr(event, "seq", 0) or 0)
        if seq <= self._cursor:
            return False
        self._cursor = seq
        session_id = _text(getattr(event, "session_id", ""), 100)
        if not session_id:
            # An event with no session cannot appear in a per-session view. It
            # still advanced the cursor, so it is not re-read forever.
            return False
        state = self._state(session_id)
        self._applied += 1

        timestamp = _number(getattr(event, "timestamp", None)) or 0.0
        if state.started_at is None:
            state.started_at = timestamp
        state.last_event_at = timestamp
        state.last_event_seq = seq
        summary = _text(getattr(event, "summary", ""))
        if summary:
            state.last_event_summary = summary
        source = _text(getattr(event, "source", ""), 60)
        if source:
            state.source = source

        kind = event.kind if isinstance(event.kind, EventKind) else None
        level = event.level if isinstance(event.level, EventLevel) else EventLevel.INFO
        data = _mapping(getattr(event, "data", None))

        entry = TimelineEntry(
            seq=seq,
            kind=kind.value if kind is not None else _text(event.kind, 40),
            level=level.value,
            source=source,
            timestamp=timestamp,
            summary=summary,
            data=self._timeline_data(kind, data),
        )
        if state.timeline.maxlen is not None and len(state.timeline) == state.timeline.maxlen:
            state.truncated_timeline += 1
        state.timeline.append(entry)

        if level is EventLevel.ERROR or kind is EventKind.ERROR:
            state.error_count += 1
            state.errors.append(entry)

        if kind is EventKind.SESSION_STATE:
            self._fold_session_state(state, data)
        elif kind is EventKind.TOOL_CALL:
            self._fold_tool_call(state, data, seq=seq, timestamp=timestamp)
        elif kind is EventKind.AGENT_ACTION:
            self._fold_agent_action(state, data)
        elif kind is EventKind.BROWSER_ACTION:
            self._fold_browser_action(state, data)
        elif kind is EventKind.RISK_DECISION:
            self._fold_risk(state, data, seq=seq)
        elif kind is EventKind.CONFIRMATION:
            self._fold_confirmation(state, data)
        elif kind is EventKind.PERFORMANCE_SPAN:
            self._fold_span(state, data)
        elif kind is EventKind.EVIDENCE:
            self._fold_evidence(state, data)

        self._sessions.move_to_end(session_id)
        self._dirty.add(session_id)
        return True

    def merge_record(self, record: Mapping[str, Any]) -> None:
        """Fold durable session facts that predate any event.

        A session created a moment ago, or restored after a restart, has a task,
        a repository and an access profile on disk and no events at all. Without
        this the browser would show nothing until the first event arrived, which
        is exactly when a user is looking.

        The mapping is the shape ``karox session list --json`` emits, so this
        works across a process boundary and does not couple the view layer to
        the session dataclass.
        """

        session_id = _text(record.get("session_id"), 100)
        if not session_id:
            return
        state = self._state(session_id)
        title = _text(record.get("task"))
        if title and not state.title:
            state.title = title
        profile = _text(record.get("access_profile"), 60)
        if profile and not state.access_profile:
            state.access_profile = profile
        mode = _text(record.get("workspace_mode"), 60)
        if mode and not state.workspace_mode:
            state.workspace_mode = mode
        # An event-reported status is newer than the file, so the record only
        # fills a status nothing has reported yet.
        status = _text(record.get("status") or record.get("phase"), 40)
        if status and state.status == "unknown":
            state.status = status
            state.terminal = self._is_terminal(status)
        created = _number(record.get("created_at"))
        if created is not None and state.started_at is None:
            state.started_at = created
        updated = _number(record.get("updated_at"))
        if updated is not None and updated > state.last_event_at:
            state.last_event_at = updated
        changed = _count(record.get("changed_files"))
        if changed is not None and changed > state.changed_files:
            state.changed_files = changed
        for key, target in (("checkpoints", state.checkpoints), ("evidence", state.evidence)):
            items = record.get(key)
            if isinstance(items, (list, tuple)) and not target:
                target.extend(
                    dict(_mapping(item)) for item in items[:MAX_LIST_ITEMS]
                )
        usage = _mapping(record.get("usage"))
        for name, value in usage.items():
            number = _number(value)
            if number is not None:
                state.usage.setdefault(_text(name, 40), number)
        self._dirty.add(session_id)

    def merge_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            self.merge_record(_mapping(record))

    # ----------------------------------------------------------------- output

    def consume_dirty(self) -> tuple[str, ...]:
        """Sessions changed since the last call, sorted, then cleared."""

        dirty = tuple(sorted(self._dirty))
        self._dirty.clear()
        return dirty

    @property
    def dirty(self) -> tuple[str, ...]:
        return tuple(sorted(self._dirty))

    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def summaries(self) -> tuple[SessionSummary, ...]:
        """Every tracked session, most recently active first.

        The tie-break on session id keeps the order total, so two sessions whose
        last event shares a sequence number cannot swap places between refreshes
        and move a row out from under the cursor.
        """

        rows = [self._summary(state) for state in self._sessions.values()]
        rows.sort(key=lambda row: (-row.last_event_seq, row.session_id))
        return tuple(rows)

    def summary(self, session_id: str) -> Optional[SessionSummary]:
        state = self._sessions.get(session_id)
        return None if state is None else self._summary(state)

    def detail(self, session_id: str) -> Optional[SessionDetail]:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        return SessionDetail(
            summary=self._summary(state),
            timeline=tuple(state.timeline),
            tool_calls=tuple(state.tool_calls.values()),
            browser=dict(state.browser),
            git=dict(state.git),
            diff=dict(state.diff),
            checkpoints=tuple(dict(item) for item in state.checkpoints),
            evidence=tuple(dict(item) for item in state.evidence),
            usage=dict(state.usage),
            costs=dict(state.costs),
            errors=tuple(state.errors),
            performance={
                name: dict(values) for name, values in sorted(state.performance.items())
            },
            pending_confirmation=state.pending_confirmation,
            dropped_events=self._dropped,
            truncated_timeline=state.truncated_timeline,
        )

    def timeline(self, session_id: str, *, since_seq: int = 0) -> tuple[TimelineEntry, ...]:
        """Timeline entries after ``since_seq``, for an incremental redraw."""

        state = self._sessions.get(session_id)
        if state is None:
            return ()
        return tuple(item for item in state.timeline if item.seq > since_seq)

    def to_dict(self) -> dict[str, Any]:
        """A bounded, already-redacted snapshot for a ``--json`` interface."""

        return {
            "cursor": self._cursor,
            "dropped_events": self._dropped,
            "evicted_sessions": self._evicted_sessions,
            "sessions": [row.to_dict() for row in self.summaries()],
        }

    # ------------------------------------------------------------- internals

    def _state(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState(
                session_id,
                timeline_limit=self._timeline_limit,
                tool_limit=self._tool_call_limit,
            )
            self._sessions[session_id] = state
            while len(self._sessions) > self._session_limit:
                evicted, _ = self._sessions.popitem(last=False)
                self._evicted_sessions += 1
                self._dirty.discard(evicted)
        return state

    @staticmethod
    def _timeline_data(
        kind: Optional[EventKind], data: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Copy only the keys this kind is allowed to display.

        An allowlist rather than a scrub: the bus already redacted the payload,
        and a second scrubber would be a second implementation to keep correct.
        A key nobody asked for simply never reaches a screen.
        """

        if kind is None:
            return {}
        allowed = _TIMELINE_KEYS.get(kind, ())
        picked: dict[str, Any] = {}
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or isinstance(value, (int, float)):
                picked[key] = value
            else:
                text = _text(value, 120)
                if text:
                    picked[key] = text
        return picked

    @staticmethod
    def _is_terminal(status: str) -> bool:
        return status in _TERMINAL_STATUSES

    def _fold_session_state(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        for key, attribute in (
            ("task", "title"),
            ("title", "title"),
            ("agent", "agent"),
            ("provider", "provider"),
            ("model", "model"),
            ("workspace_mode", "workspace_mode"),
            ("access_profile", "access_profile"),
            ("current_step", "current_step"),
            ("waiting_reason", "waiting_reason"),
        ):
            if key in data:
                text = _text(data[key])
                if text or key in {"waiting_reason", "current_step"}:
                    setattr(state, attribute, text)
        status = _text(data.get("status") or data.get("phase"), 40)
        if status:
            state.status = status
            state.terminal = self._is_terminal(status)
            if status not in _WAITING_STATUSES and "waiting_reason" not in data:
                state.waiting_reason = ""
        changed = _count(data.get("changed_files"))
        if changed is not None:
            state.changed_files = changed
        git = _mapping(data.get("git"))
        if git:
            state.git = {
                _text(key, 40): _text(value, 120) for key, value in git.items()
            }
        diff = _mapping(data.get("diff"))
        if diff:
            state.diff = {}
            for key, value in diff.items():
                number = _number(value)
                state.diff[_text(key, 40)] = (
                    number if number is not None else _text(value, 120)
                )

    def _fold_tool_call(
        self,
        state: _SessionState,
        data: Mapping[str, Any],
        *,
        seq: int,
        timestamp: float,
    ) -> None:
        name = _text(data.get("tool") or data.get("name"), 120)
        call_id = _text(data.get("call_id"), 120) or name or f"seq-{seq}"
        phase = _text(data.get("phase"), 20)
        existing = state.tool_calls.get(call_id)
        finished = phase == "finished" or "ok" in data or "duration_ms" in data
        if not finished and phase != "finished":
            state.note_tool_call(
                ToolCallView(
                    call_id=call_id,
                    name=name or (existing.name if existing else call_id),
                    started_seq=existing.started_seq if existing else seq,
                    started_at=existing.started_at if existing else timestamp,
                    detail=_text(data.get("detail"), 120),
                )
            )
            state.current_step = name or state.current_step
            return
        duration = _number(data.get("duration_ms"))
        started_at = existing.started_at if existing else timestamp
        if duration is None and existing is not None and timestamp >= started_at:
            duration = round((timestamp - started_at) * 1000.0, 3)
        state.note_tool_call(
            ToolCallView(
                call_id=call_id,
                name=name or (existing.name if existing else call_id),
                started_seq=existing.started_seq if existing else seq,
                started_at=started_at,
                finished_at=timestamp,
                duration_ms=duration,
                ok=_flag(data.get("ok")),
                detail=_text(data.get("detail"), 120),
            )
        )

    def _fold_agent_action(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        step = _text(data.get("step"))
        if step:
            state.current_step = step
        usage = _mapping(data.get("usage"))
        for key, value in usage.items():
            number = _number(value)
            if number is not None:
                state.usage[_text(key, 40)] = number
        cost = _mapping(data.get("cost") or data.get("costs"))
        for currency, amount in cost.items():
            number = _number(amount)
            if number is not None:
                state.costs[_text(currency, 12)] = number
        # ``budgets``, not ``token_budget``: see the naming constraint in the
        # module docstring. A key containing "token" is blanked by the redactor
        # before this layer ever sees it.
        budgets = _mapping(data.get("budgets"))
        state.token_budget = self._budget(
            budgets.get("usage"),
            fallback_used=state.usage.get("total_tokens"),
            current=state.token_budget,
            unit="tokens",
        )
        state.cost_budget = self._budget(
            budgets.get("cost"),
            fallback_used=sum(state.costs.values()) if state.costs else None,
            current=state.cost_budget,
            unit=next(iter(sorted(state.costs))) if state.costs else "",
        )

    @staticmethod
    def _budget(
        raw: Any,
        *,
        fallback_used: Optional[float],
        current: BudgetView,
        unit: str,
    ) -> BudgetView:
        payload = _mapping(raw)
        used = _number(payload.get("used"))
        if used is None:
            used = fallback_used if fallback_used is not None else current.used
        limit = _number(payload.get("limit"))
        if limit is None:
            limit = current.limit
        return BudgetView(
            used=used,
            limit=limit,
            unit=_text(payload.get("unit"), 12) or unit or current.unit,
        )

    def _fold_browser_action(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        for key in (
            "action",
            "tab_id",
            "url",
            "origin",
            "navigation_generation",
            "takeover",
            "context_id",
        ):
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or isinstance(value, (int, float)):
                state.browser[key] = value
            else:
                state.browser[key] = _text(value, 200)

    def _fold_risk(self, state: _SessionState, data: Mapping[str, Any], *, seq: int) -> None:
        allowed = _flag(data.get("allowed"))
        view = RiskStateView(
            seq=seq,
            level=_text(data.get("risk"), 20),
            allowed=bool(allowed),
            reason=_text(data.get("reason"), 60),
            action_digest=_text(data.get("action_digest"), 64),
            reasons=_string_list(data.get("reasons")),
            preview=dict(_mapping(data.get("preview"))),
        )
        state.risk = view
        if view.awaiting_confirmation:
            state.pending_confirmation = view
            state.waiting_reason = WAIT_CONFIRMATION
        elif state.pending_confirmation is not None and (
            not view.action_digest
            or view.action_digest == state.pending_confirmation.action_digest
        ):
            # The same action came back with a verdict, so nothing is pending.
            state.pending_confirmation = None
            if state.waiting_reason == WAIT_CONFIRMATION:
                state.waiting_reason = ""

    def _fold_confirmation(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        decision = _text(data.get("decision"), 20)
        if decision in {"approved", "rejected", "expired", "cancelled"}:
            state.pending_confirmation = None
            if state.waiting_reason == WAIT_CONFIRMATION:
                state.waiting_reason = ""

    def _fold_span(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        component = _text(data.get("component"), 40) or "karox"
        duration = _number(data.get("duration_ms"))
        if duration is None:
            return
        bucket = state.performance.setdefault(
            component, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0}
        )
        bucket["count"] += 1.0
        bucket["total_ms"] = round(bucket["total_ms"] + duration, 3)
        bucket["max_ms"] = max(bucket["max_ms"], duration)

    def _fold_evidence(self, state: _SessionState, data: Mapping[str, Any]) -> None:
        record = {
            key: _text(data[key], 200)
            for key in ("evidence_id", "kind", "summary")
            if key in data
        }
        if record:
            state.evidence.append(record)
            del state.evidence[:-MAX_LIST_ITEMS]

    def _summary(self, state: _SessionState) -> SessionSummary:
        started = state.started_at
        if started is None:
            elapsed = 0.0
        elif state.terminal:
            elapsed = max(state.last_event_at - started, 0.0)
        else:
            elapsed = max(float(self._now()) - started, 0.0)
        return SessionSummary(
            session_id=state.session_id,
            title=state.title,
            agent=state.agent,
            source=state.source,
            provider=state.provider,
            model=state.model,
            workspace_mode=state.workspace_mode,
            access_profile=state.access_profile,
            status=state.status,
            current_step=state.current_step,
            changed_files=state.changed_files,
            token_budget=state.token_budget,
            cost_budget=state.cost_budget,
            elapsed_seconds=round(elapsed, 3),
            waiting_reason=state.waiting_reason,
            last_event_summary=state.last_event_summary,
            last_event_seq=state.last_event_seq,
            last_event_at=state.last_event_at,
            primary_action=self._primary_action(state),
            error_count=state.error_count,
            risk=state.risk,
        )

    @staticmethod
    def _primary_action(state: _SessionState) -> str:
        """The one action the row offers, as a stable identifier.

        A row with five buttons is a row nobody reads. A pending confirmation
        outranks everything because it is the only state where the agent is
        stopped and waiting on the person looking at the screen.
        """

        if state.pending_confirmation is not None:
            return ACTION_REVIEW_RISK
        if state.terminal:
            return ACTION_OPEN
        if state.status in _WAITING_STATUSES:
            return ACTION_RESUME
        if state.status in _RUNNING_STATUSES:
            return ACTION_STOP
        return ACTION_OPEN


__all__ = [
    "ACTION_OPEN",
    "ACTION_RESUME",
    "ACTION_REVIEW_RISK",
    "ACTION_STOP",
    "BudgetView",
    "DEFAULT_SESSION_LIMIT",
    "DEFAULT_TIMELINE_LIMIT",
    "DEFAULT_TOOL_CALL_LIMIT",
    "RiskStateView",
    "SessionDetail",
    "SessionSummary",
    "SessionViewStore",
    "TimelineEntry",
    "ToolCallView",
    "WAIT_CONFIRMATION",
]
