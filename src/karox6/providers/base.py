"""Provider contract shared by every model adapter.

An adapter turns a :class:`ModelRequest` into one HTTP call (streaming where the
API supports it) and returns one assistant :class:`Message` plus usage. Adapters
are thin: no prompt logic, no retries of their own — retries live in
:func:`complete_with_retry` so every provider behaves the same.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
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

    A server ``Retry-After`` is honoured up to ``max_retry_after_s``. Rate limits are
    different: a provider can refuse a key for minutes (e.g. a per-key limit on client
    addresses in a 10-minute window), so ``rate_limit`` errors are retried until
    ``rate_limit_budget_s`` of waiting is spent, with delays growing to
    ``rate_limit_max_delay_s``. A request refused with 429 was not executed, so
    waiting it out is always safe; the session deadline still bounds it. "Service
    unavailable" answers (502, 503, 529: the request was not processed either) are
    waited out the same way on ``unavailable_budget_s``, so an upstream outage of a few
    minutes does not end the run; other server errors and timeouts keep the short policy.
    """

    max_retries: int = 5
    initial_delay_s: float = 0.5
    max_delay_s: float = 10.0
    jitter: float = 0.1
    max_retry_after_s: float = 60.0
    rate_limit_budget_s: float = 900.0
    rate_limit_max_delay_s: float = 60.0
    unavailable_budget_s: float = 600.0

    def delay(self, attempt: int, retry_after: float | None, *, rate_limit: bool = False) -> float:
        if retry_after is not None and 0 < retry_after <= self.max_retry_after_s:
            return retry_after
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
    attempt = 0
    waited = {"rate_limit": 0.0, "unavailable": 0.0}
    while True:
        try:
            response = await provider.complete(request, on_delta)
        except ProviderError as exc:
            attempt += 1
            if exc.code not in RETRYABLE:
                raise
            kind = (
                "rate_limit"
                if exc.code == "rate_limit"
                else "unavailable"
                if exc.status in UNAVAILABLE_STATUSES
                else None
            )
            if kind is not None:
                budget = (
                    policy.rate_limit_budget_s
                    if kind == "rate_limit"
                    else policy.unavailable_budget_s
                )
                delay = policy.delay(attempt, exc.retry_after, rate_limit=True)
                if waited[kind] + delay > budget:
                    raise
                waited[kind] += delay
            elif attempt > policy.max_retries:
                raise
            else:
                delay = policy.delay(attempt, exc.retry_after)
            if on_retry is not None:
                maybe = on_retry(attempt, delay, exc)
                if maybe is not None:
                    await maybe
            await asyncio.sleep(delay)
            continue
        return response


def is_empty(message: Message) -> bool:
    return not message.content.strip() and not message.tool_calls
