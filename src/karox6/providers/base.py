"""Provider contract shared by every model adapter.

An adapter turns a :class:`ModelRequest` into one HTTP call (streaming where the
API supports it) and returns one assistant :class:`Message` plus usage. Adapters
are thin: no prompt logic, no retries of their own — retries live in
:func:`complete_with_retry` so every provider behaves the same.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from karox6.core.messages import Message, Usage

ErrorCode = Literal[
    "rate_limit",
    "server",
    "timeout",
    "transport",
    "empty_response",
    "context_window",
    "auth",
    "quota",  # balance or daily allowance used up: waiting does not help
    "bad_request",
    "cancelled",
]
RETRYABLE: frozenset[str] = frozenset(
    {"rate_limit", "server", "timeout", "transport", "empty_response"}
)
UNAVAILABLE_STATUSES = frozenset({502, 503, 529})  # not processed: safe to wait out


class ProviderError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        status = f" (HTTP {self.status})" if self.status else ""
        return f"{self.code}{status}: {self.args[0]}"


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ModelRequest:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelResponse:
    message: Message  # role == "assistant"
    usage: Usage
    finish_reason: str  # normalized: stop | tool_calls | length | content_filter | other
    model: str | None = None


@dataclass(slots=True)
class Delta:
    kind: Literal["text", "reasoning"]
    text: str


DeltaCallback = Callable[[Delta], None]


class Provider(Protocol):
    name: str

    async def complete(
        self, request: ModelRequest, on_delta: DeltaCallback | None = None
    ) -> ModelResponse: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class RetryPolicy:
    """Defaults follow DSH (dsh-llm): 5 retries, 0.5 s doubling to 10 s, +-10% jitter.

    Rate limits are different from other failures: a provider can refuse a key for
    minutes (e.g. a per-key limit on client addresses in a 10-minute window), so
    ``rate_limit`` errors are retried until ``rate_limit_budget_s`` of waiting is spent,
    with delays growing to ``rate_limit_max_delay_s``. A request refused with 429 was not
    executed, so waiting it out is always safe; the session deadline still bounds it.
    "Service unavailable" answers (502, 503, 529: the request was not processed either)
    are waited out the same way on ``unavailable_budget_s``, so an upstream outage of a
    few minutes does not end the run; other server errors and timeouts keep the short
    policy.

    A server's ``Retry-After`` is a floor, never shortened: asking again earlier is
    refused again and can extend the ban. For rate limits and outages it is honoured
    whatever its length while the waiting budget lasts (a wait longer than the budget
    left ends the retries at once: sleeping would not help); for other errors up to
    ``max_retry_after_s``. A little jitter is added on top, so several agents on one
    key do not all come back in the same instant.

    An empty reply is asked for again only ``empty_retries`` times: the same request
    tends to get the same empty answer (some providers cache identical requests), and
    every try is billed for the whole conversation. The session then changes the request
    (a notice to the model) instead.
    """

    max_retries: int = 5
    initial_delay_s: float = 0.5
    max_delay_s: float = 10.0
    jitter: float = 0.1
    max_retry_after_s: float = 60.0
    rate_limit_budget_s: float = 900.0
    rate_limit_max_delay_s: float = 60.0
    unavailable_budget_s: float = 600.0
    empty_retries: int = 1

    def delay(self, attempt: int, retry_after: float | None, *, rate_limit: bool = False) -> float:
        if (
            retry_after is not None
            and retry_after > 0
            and (rate_limit or retry_after <= self.max_retry_after_s)
        ):
            # Never earlier than asked; up to 10% (at most 2 s) later.
            return retry_after + min(retry_after * self.jitter, 2.0) * random.random()
        cap = self.rate_limit_max_delay_s if rate_limit else self.max_delay_s
        base = min(self.initial_delay_s * 2.0 ** (attempt - 1), cap)
        return base * (1 - self.jitter + 2 * self.jitter * random.random())


RetryCallback = Callable[[int, float, ProviderError], Awaitable[None] | None]


async def complete_with_retry(
    provider: Provider,
    request: ModelRequest,
    on_delta: DeltaCallback | None = None,
    policy: RetryPolicy | None = None,
    on_retry: RetryCallback | None = None,
) -> ModelResponse:
    policy = policy or RetryPolicy()
    attempt = 0  # all retries, for on_retry
    # Each kind of failure has its own count: a long 429 or 503 spell must not use up the
    # few quick retries a dropped connection or a plain 500 gets afterwards.
    tries = {"rate_limit": 0, "unavailable": 0, "empty": 0, "other": 0}
    waited = {"rate_limit": 0.0, "unavailable": 0.0}
    while True:
        try:
            response = await provider.complete(request, on_delta)
        except ProviderError as exc:
            if exc.code not in RETRYABLE:
                raise
            attempt += 1
            kind = (
                "rate_limit"
                if exc.code == "rate_limit"
                else "unavailable"
                if exc.status in UNAVAILABLE_STATUSES
                else "empty"
                if exc.code == "empty_response"
                else "other"
            )
            tries[kind] += 1
            if kind in ("other", "empty"):
                limit = policy.max_retries if kind == "other" else policy.empty_retries
                if tries[kind] > min(limit, policy.max_retries):
                    raise
                delay = policy.delay(tries[kind], exc.retry_after)
            else:
                budget = (
                    policy.rate_limit_budget_s
                    if kind == "rate_limit"
                    else policy.unavailable_budget_s
                )
                delay = policy.delay(tries[kind], exc.retry_after, rate_limit=True)
                if waited[kind] + delay > budget:
                    raise
                waited[kind] += delay
            if on_retry is not None:
                maybe = on_retry(attempt, delay, exc)
                if maybe is not None:
                    await maybe
            await asyncio.sleep(delay)
            continue
        return response


def is_empty(message: Message) -> bool:
    return not message.content.strip() and not message.tool_calls


STREAM_IDLE_S = 300.0


class StreamStalled(ProviderError):
    """The reply's stream went silent after its text or tool calls had started."""

    def __init__(self, idle_s: float) -> None:
        super().__init__("timeout", f"the reply stream stalled: no data for {idle_s:.0f} s")


async def stream_lines(
    response: Any, idle_s: float | None, flowing: Callable[[], bool]
) -> AsyncIterator[str]:
    """The lines of a streamed HTTP response. Once the answer itself is flowing
    (``flowing()``: text or tool-call tokens have arrived), a silence of ``idle_s``
    seconds raises :class:`StreamStalled`, which the retry policy treats as a timeout:
    a connection that died without a reset (a sleeping laptop, a dropped NAT entry) no
    longer hangs for the whole request timeout. Before that point the model may be
    thinking without sending anything, so only the request timeout applies. Keep-alive
    lines (SSE comments, pings, blank separators) do not count as progress: a server that
    pings every 30 s while the answer is stuck still stalls after ``idle_s``."""
    lines = response.aiter_lines().__aiter__()
    quiet_since: float | None = None
    while True:
        try:
            if idle_s and flowing():
                if quiet_since is None:
                    quiet_since = time.monotonic()
                left = idle_s - (time.monotonic() - quiet_since)
                if left <= 0:
                    raise TimeoutError
                async with asyncio.timeout(left):
                    line = await anext(lines)
            else:
                quiet_since = None
                line = await anext(lines)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise StreamStalled(idle_s or 0.0) from None
        if not _keep_alive(line):
            quiet_since = None
        yield line


_PING_DATA = re.compile(r'^data:\s*\{\s*"type"\s*:\s*"ping"\s*\}$')


def _keep_alive(line: str) -> bool:
    """An SSE line that carries no part of the answer: blank, a comment, an event name
    (its data line follows) or a ping event."""
    text = line.strip()
    return (
        not text
        or text.startswith(":")
        or text.startswith("event:")
        or bool(_PING_DATA.match(text))
    )


_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_BODY_WAIT = (
    # "Please try again in 1.242s", "retry after 20 seconds", "Try again in 3 minutes"
    re.compile(
        r"(?:try|retry)\s+(?:again\s+)?(?:in|after)\s+(\d+(?:\.\d+)?)\s*"
        r"(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hours?)\b",
        re.I,
    ),
    # Gemini's RetryInfo ("retryDelay": "23s") and JSON bodies with retry_after: 30
    re.compile(r'"retry[_-]?(?:delay|after)"\s*:\s*"?(\d+(?:\.\d+)?)(ms|s|m|h)?', re.I),
)
MAX_SERVER_WAIT_S = 86_400.0  # a longer "wait" is a parsing accident, not advice


def retry_after_seconds(headers: Any, text: str = "") -> float | None:
    """How long the server asks to wait before the next try, in seconds (None: it does
    not say). Read from ``Retry-After`` (seconds or an HTTP date), ``retry-after-ms``,
    OpenAI-style ``x-ratelimit-reset-*`` durations ("6m0s"), a generic
    ``x-ratelimit-reset`` (seconds, or an epoch in seconds or milliseconds), and wording
    in the error body ("try again in 1.2s", Gemini's ``retryDelay``)."""
    get = headers.get if hasattr(headers, "get") else (lambda _name: None)
    found = (
        _number(get("retry-after-ms"), scale=0.001)
        or _retry_after_header(get("retry-after"), get("date"))
        or _reset_headers(get)
        or _body_wait(text)
    )
    if found is None or not 0 < found <= MAX_SERVER_WAIT_S:
        return None
    return found


def _number(raw: Any, scale: float = 1.0) -> float | None:
    try:
        return float(str(raw).strip()) * scale if raw is not None else None
    except ValueError:
        return None


def _retry_after_header(raw: Any, date: Any) -> float | None:
    if raw is None:
        return None
    seconds = _number(raw)
    if seconds is not None:
        return seconds
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(str(raw))
        # The server's own clock when it sent the date, if it says so: local clocks drift.
        now = parsedate_to_datetime(str(date)) if date else None
    except (TypeError, ValueError):
        return None
    if now is None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
    try:
        return (when - now).total_seconds()
    except TypeError:  # one of them has no time zone
        return None


def _duration(raw: Any) -> float | None:
    """OpenAI's reset durations: "1s", "250ms", "6m0s", "1h2m3s"; a bare number is seconds."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    plain = _number(text)
    if plain is not None:
        return plain
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)


def _reset_headers(get: Callable[[str], Any]) -> float | None:
    waits = []
    for kind in ("requests", "tokens"):
        reset = _duration(get(f"x-ratelimit-reset-{kind}"))
        remaining = _number(get(f"x-ratelimit-remaining-{kind}"))
        # Only the limit that is used up tells when to come back.
        if reset is not None and (remaining is None or remaining <= 0):
            waits.append(reset)
    if waits:
        return max(waits)
    generic = _number(get("x-ratelimit-reset"))
    if generic is None:
        return None
    if generic > 1e11:  # epoch milliseconds
        return generic / 1000.0 - time.time()
    if generic > 1e9:  # epoch seconds
        return generic - time.time()
    return generic


def _body_wait(text: str) -> float | None:
    for pattern in _BODY_WAIT:
        match = pattern.search(text)
        if match:
            unit = (match.group(2) or "s").lower()
            key = "ms" if unit.startswith(("ms", "mil")) else unit[0]
            return float(match.group(1)) * _UNIT_SECONDS[key]
    return None
