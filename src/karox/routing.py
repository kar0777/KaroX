"""Explicit provider routing with fail-closed capabilities and hard budgets."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Protocol

from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    accumulate_response,
)
from .registry import ModelRecord, ProviderRecord, ProviderRegistry


_PRIVACY_RANK = {"local": 0, "private": 1, "public": 2}
# Retrying the same route and moving to the next one answer the same question —
# can another attempt help at all? — so both read one set. Everything outside it
# (authentication, an invalid request, an exhausted budget) is deterministic:
# repeating it only spends the caller's deadline.
_TRANSIENT_ERRORS = frozenset(
    {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.MODEL_UNAVAILABLE,
        ProviderErrorKind.TRANSPORT,
        ProviderErrorKind.PROVIDER_INTERNAL,
    }
)


class ProviderBuilder(Protocol):
    def create(self, record: ProviderRecord) -> Provider: ...


@dataclass(frozen=True)
class RouteTarget:
    provider_id: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("route provider ID is required")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("route model is required")


@dataclass(frozen=True)
class RetryPolicy:
    """How often one route may be re-sent before the router gives up on it."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("retry attempts must be between 1 and 10")
        for label, value in (
            ("base retry delay", self.base_delay_seconds),
            ("maximum retry delay", self.max_delay_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{label} must be finite and non-negative")
        if float(self.max_delay_seconds) < float(self.base_delay_seconds):
            raise ValueError("maximum retry delay must not be below the base delay")


@dataclass(frozen=True)
class RoutingPolicy:
    routes: tuple[RouteTarget, ...]
    privacy_limit: str = "public"
    max_total_tokens: int | None = None
    max_cost: float | None = None
    currency: str | None = None
    require_tools: bool = True
    require_streaming: bool = True
    retry: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("routing requires at least one explicit route")
        if not isinstance(self.retry, RetryPolicy):
            raise ValueError("retry policy must be a RetryPolicy")
        if self.privacy_limit not in _PRIVACY_RANK:
            raise ValueError("privacy limit must be local, private, or public")
        if self.max_total_tokens is not None and (
            isinstance(self.max_total_tokens, bool)
            or not isinstance(self.max_total_tokens, int)
            or self.max_total_tokens <= 0
        ):
            raise ValueError("token budget must be a positive integer")
        if self.max_cost is not None and (
            isinstance(self.max_cost, bool)
            or not isinstance(self.max_cost, (int, float))
            or not math.isfinite(float(self.max_cost))
            or float(self.max_cost) <= 0
        ):
            raise ValueError("cost budget must be finite and positive")
        if self.max_cost is not None and self.currency is None:
            raise ValueError("cost budget requires a currency")
        if self.currency is not None and (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("budget currency must be a three-letter uppercase code")


def _usage_total(usage: Mapping[str, Any]) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    selected = []
    for aliases in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        value = 0
        for name in aliases:
            candidate = usage.get(name)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                value = candidate
                break
        selected.append(value)
    return sum(selected)


def _merge_usage(base: Mapping[str, Any], addition: Mapping[str, Any]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for source in (base, addition):
        for name, raw_count in source.items():
            if (
                isinstance(name, str)
                and isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and raw_count >= 0
            ):
                merged[name] = merged.get(name, 0) + raw_count
    merged["total_tokens"] = _usage_total(base) + _usage_total(addition)
    return merged


class RoutedProvider:
    """Try only declared routes and expose a durable, secret-free audit trail.

    A transient failure is retried on the same route, with jittered exponential
    backoff bounded by the request deadline, before the next route is tried at
    all: a two-second rate limit should not cost a twenty-minute task, and a
    single-route configuration has no next route to fall back to.
    """

    provider_name = "routed_provider"

    def __init__(
        self,
        registry: ProviderRegistry,
        factory: ProviderBuilder | Callable[[ProviderRecord], Provider],
        policy: RoutingPolicy,
        *,
        initial_usage: Mapping[str, Any] | None = None,
        initial_costs: Mapping[str, Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.registry = registry
        self.factory = factory
        self.policy = policy
        # Waiting and telling the time are injected so a test can assert the
        # backoff it would have slept instead of spending it, and so the
        # deadline is measured on a clock that cannot jump backwards.
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._jitter = jitter if jitter is not None else random.random
        self._usage = _merge_usage({}, initial_usage or {})
        self._costs: Dict[str, float] = {}
        for currency, raw_cost in (initial_costs or {}).items():
            if (
                isinstance(currency, str)
                and isinstance(raw_cost, (int, float))
                and not isinstance(raw_cost, bool)
                and math.isfinite(float(raw_cost))
                and float(raw_cost) >= 0
            ):
                self._costs[currency] = float(raw_cost)

    def _provider(self, record: ProviderRecord) -> Provider:
        create = getattr(self.factory, "create", None)
        if callable(create):
            return create(record)
        if callable(self.factory):
            return self.factory(record)
        raise TypeError("provider factory must be callable or expose create()")

    def _check_global_budgets(self) -> None:
        if (
            self.policy.max_total_tokens is not None
            and _usage_total(self._usage) >= self.policy.max_total_tokens
        ):
            raise ProviderError(
                ProviderErrorKind.BUDGET_EXCEEDED,
                "token budget is already exhausted",
            )
        if self.policy.max_cost is not None:
            assert self.policy.currency is not None
            if self._costs.get(self.policy.currency, 0.0) >= self.policy.max_cost:
                raise ProviderError(
                    ProviderErrorKind.BUDGET_EXCEEDED,
                    "cost budget is already exhausted",
                )

    def _preflight(
        self, target: RouteTarget, request: ModelRequest
    ) -> tuple[ProviderRecord, ModelRecord]:
        provider = self.registry.provider(target.provider_id)
        model = self.registry.model(target.provider_id, target.model)
        if _PRIVACY_RANK[provider.privacy_class] > _PRIVACY_RANK[self.policy.privacy_limit]:
            raise ProviderError(
                ProviderErrorKind.PERMISSION,
                f"route privacy class {provider.privacy_class} exceeds "
                f"{self.policy.privacy_limit}",
            )
        if request.tools and self.policy.require_tools and model.tools != "true":
            raise ProviderError(
                ProviderErrorKind.UNSUPPORTED_CAPABILITY,
                "route model does not explicitly support tools",
            )
        if self.policy.require_streaming and model.streaming != "true":
            raise ProviderError(
                ProviderErrorKind.UNSUPPORTED_CAPABILITY,
                "route model does not explicitly support streaming",
            )
        if self.policy.max_cost is not None:
            if model.pricing is None:
                raise ProviderError(
                    ProviderErrorKind.BUDGET_EXCEEDED,
                    "cost budget requires model pricing",
                )
            assert self.policy.currency is not None
            if model.pricing.currency != self.policy.currency:
                raise ProviderError(
                    ProviderErrorKind.BUDGET_EXCEEDED,
                    "model pricing currency does not match the cost budget",
                )
        return provider, model

    def _retry_delay(
        self, error: ProviderError, attempts: int, deadline: float
    ) -> Optional[float]:
        """Return how long to wait before re-sending, or None to stop trying."""
        policy = self.policy.retry
        if error.kind not in _TRANSIENT_ERRORS or attempts >= policy.max_attempts:
            return None
        supplied = error.retry_after
        if (
            isinstance(supplied, (int, float))
            and not isinstance(supplied, bool)
            and math.isfinite(float(supplied))
        ):
            # The provider knows when its own quota resets, so its number is
            # the starting point -- but it is still bounded here. A 429 may
            # carry Retry-After: 3500, and the sleep is uninterruptible, so
            # honouring it literally parks a run for an hour with no way to
            # stop it; past the cap the caller is better served by the router
            # moving on. A clock skewed forward makes the date form parse to
            # zero, so the floor keeps a retry from becoming an immediate
            # re-send of the request that was just rejected.
            asked = float(supplied)
            if asked <= 0.0:
                # A clock skewed forward makes the HTTP-date form parse to zero,
                # which would turn a retry into an immediate re-send of the
                # request that was just rejected.
                asked = float(policy.base_delay_seconds)
            # Jitter is added on top, never subtracted: waiting less than the
            # provider asked for only earns another rejection, but every
            # concurrent run is handed the same number and would otherwise wake
            # together and re-hit one shared quota in lockstep.
            delay = min(
                float(policy.max_delay_seconds),
                asked * (1.0 + 0.5 * self._jitter()),
            )
        else:
            window = min(
                float(policy.max_delay_seconds),
                float(policy.base_delay_seconds) * 2 ** (attempts - 1),
            )
            # Half the window is fixed so a retry always backs off by something.
            delay = window * (0.5 + 0.5 * self._jitter())
        # Sleeping past the caller's deadline turns a recoverable rate limit
        # into a slower, quieter failure than not retrying at all.
        if delay >= deadline - self._monotonic():
            return None
        return delay

    @staticmethod
    def _attempt(
        index: int,
        target: RouteTarget,
        *,
        model_id: str | None = None,
        status: str,
        error: ProviderError | None = None,
        retries: int = 0,
    ) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "route_index": index,
            "provider_id": target.provider_id,
            "model": model_id or target.model,
            "status": status,
        }
        if retries:
            value["retries"] = retries
        if error is not None:
            value["error_kind"] = error.kind.value
            value["message"] = error.safe_message
            value["status_code"] = error.status_code
        return value

    def _decorate(
        self,
        response: ModelResponse,
        *,
        attempts: list[Dict[str, Any]],
        provider_record: ProviderRecord,
        model_record: ModelRecord,
    ) -> ModelResponse:
        """Charge a completed response to the session and stamp what it cost."""

        self._usage = _merge_usage(self._usage, response.usage)
        cost = None
        currency = None
        pricing_version = None
        cumulative_cost = None
        if model_record.pricing is not None:
            cost = model_record.pricing.estimate(response.usage)
            currency = model_record.pricing.currency
            pricing_version = model_record.pricing.version
            self._costs[currency] = round(self._costs.get(currency, 0.0) + cost, 12)
            cumulative_cost = self._costs[currency]

        exceeded: list[str] = []
        if (
            self.policy.max_total_tokens is not None
            and _usage_total(self._usage) > self.policy.max_total_tokens
        ):
            exceeded.append("token_budget")
        if self.policy.max_cost is not None:
            assert self.policy.currency is not None
            if self._costs.get(self.policy.currency, 0.0) > self.policy.max_cost:
                exceeded.append("cost_budget")

        return replace(
            response,
            route_attempts=tuple(attempts),
            selected_provider=provider_record.provider_id,
            selected_model=model_record.model_id,
            cost=cost,
            currency=currency,
            pricing_version=pricing_version,
            cumulative_usage=dict(self._usage),
            cumulative_cost=cumulative_cost,
            budget_exceeded=bool(exceeded),
            budget_reason=",".join(exceeded) or None,
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        """Forward the selected route's own events as they arrive.

        Routing, retry and budget accounting are unchanged; what changes is when
        the caller learns anything. A route may still be retried or fall back,
        but only while it has emitted nothing — once a delta has reached the
        caller, re-sending would replay text it has already been shown, so that
        failure is surfaced instead of papered over. The terminal ``COMPLETION``
        event carries the fully decorated response, so a streaming consumer sees
        the same route, cost and budget verdict a blocking one does.
        """

        self._check_global_budgets()
        deadline = self._monotonic() + float(request.deadline_seconds)
        attempts: list[Dict[str, Any]] = []
        last_rejection: ProviderError | None = None
        for index, target in enumerate(self.policy.routes):
            try:
                provider_record, model_record = self._preflight(target, request)
            except ProviderError as exc:
                attempts.append(
                    self._attempt(index, target, status="rejected", error=exc)
                )
                last_rejection = exc
                continue

            routed_request = self._routed_request(request, model_record)
            provider: Provider | None = None
            failure: ProviderError | None = None
            retries = 0
            emitted = False
            collected: list[ModelEvent] = []
            while True:
                collected = []
                try:
                    if provider is None:
                        provider = self._provider(provider_record)
                    for event in provider.stream(routed_request):
                        collected.append(event)
                        if event.kind is ModelEventKind.COMPLETION:
                            # Held back: the caller gets exactly one terminal
                            # event, the decorated one below, rather than the
                            # transport's bare completion followed by ours.
                            continue
                        emitted = True
                        yield event
                except ProviderError as exc:
                    # A partly delivered turn cannot be retried: the caller has
                    # already been shown the first half of an answer it would
                    # then be shown again.
                    delay = (
                        None if emitted else self._retry_delay(exc, retries + 1, deadline)
                    )
                    if delay is None:
                        failure = exc
                        break
                    retries += 1
                    self._sleep(delay)
                    continue
                break

            if failure is not None:
                can_fallback = (
                    not emitted
                    and failure.kind in _TRANSIENT_ERRORS
                    and index + 1 < len(self.policy.routes)
                )
                attempts.append(
                    self._attempt(
                        index,
                        target,
                        model_id=model_record.model_id,
                        status="fallback" if can_fallback else "failed",
                        error=failure,
                        retries=retries,
                    )
                )
                if can_fallback:
                    continue
                raise ProviderError(
                    failure.kind,
                    failure.safe_message,
                    status_code=failure.status_code,
                    retry_after=failure.retry_after,
                    route_attempts=tuple(attempts),
                ) from failure

            attempts.append(
                self._attempt(
                    index,
                    target,
                    model_id=model_record.model_id,
                    status="completed",
                    retries=retries,
                )
            )
            # Folding the events the route already produced costs nothing and
            # keeps one definition of what a stream adds up to.
            response = accumulate_response(iter(collected))
            decorated = self._decorate(
                response,
                attempts=attempts,
                provider_record=provider_record,
                model_record=model_record,
            )
            yield ModelEvent(
                ModelEventKind.COMPLETION,
                finish_reason=decorated.finish_reason,
                response_id=decorated.response_id,
                transport_attempts=decorated.transport_attempts,
                response=decorated,
            )
            return
        if last_rejection is not None:
            raise ProviderError(
                last_rejection.kind,
                last_rejection.safe_message,
                status_code=last_rejection.status_code,
                retry_after=last_rejection.retry_after,
                route_attempts=tuple(attempts),
            ) from last_rejection
        raise AssertionError("routing policy unexpectedly had no routes")

    def _routed_request(
        self, request: ModelRequest, model_record: ModelRecord
    ) -> ModelRequest:
        # The registry records each model's output ceiling; routing used to
        # rewrite only the model id, so the ceiling was never sent. The
        # Anthropic adapter then fell back to its own 4096-token default and
        # truncated long work mid-answer. An explicit request value still wins,
        # because the caller is closer to the task than the registry.
        return replace(
            request,
            model=model_record.model_id,
            max_output_tokens=(
                request.max_output_tokens
                if request.max_output_tokens is not None
                else model_record.max_output_tokens
            ),
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_global_budgets()
        # One deadline for the whole routed call, retries included: a wait that
        # outlives the caller's own timeout is never worth taking.
        deadline = self._monotonic() + float(request.deadline_seconds)
        attempts: list[Dict[str, Any]] = []
        last_rejection: ProviderError | None = None
        for index, target in enumerate(self.policy.routes):
            try:
                provider_record, model_record = self._preflight(target, request)
            except ProviderError as exc:
                attempts.append(
                    self._attempt(index, target, status="rejected", error=exc)
                )
                last_rejection = exc
                continue

            routed_request = self._routed_request(request, model_record)
            provider: Provider | None = None
            response: ModelResponse | None = None
            failure: ProviderError | None = None
            retries = 0
            while True:
                try:
                    if provider is None:
                        # Built once per route: a retry is the same endpoint
                        # again, not a new routing decision, and rebuilding it
                        # would resolve the credential over and over.
                        provider = self._provider(provider_record)
                    response = provider.complete(routed_request)
                except ProviderError as exc:
                    delay = self._retry_delay(exc, retries + 1, deadline)
                    if delay is None:
                        failure = exc
                        break
                    retries += 1
                    self._sleep(delay)
                    continue
                break

            if failure is not None:
                can_fallback = (
                    failure.kind in _TRANSIENT_ERRORS
                    and index + 1 < len(self.policy.routes)
                )
                attempts.append(
                    self._attempt(
                        index,
                        target,
                        model_id=model_record.model_id,
                        status="fallback" if can_fallback else "failed",
                        error=failure,
                        retries=retries,
                    )
                )
                if can_fallback:
                    continue
                raise ProviderError(
                    failure.kind,
                    failure.safe_message,
                    status_code=failure.status_code,
                    retry_after=failure.retry_after,
                    route_attempts=tuple(attempts),
                ) from failure

            assert response is not None
            attempts.append(
                self._attempt(
                    index,
                    target,
                    model_id=model_record.model_id,
                    status="completed",
                    retries=retries,
                )
            )
            return self._decorate(
                response,
                attempts=attempts,
                provider_record=provider_record,
                model_record=model_record,
            )
        if last_rejection is not None:
            raise ProviderError(
                last_rejection.kind,
                last_rejection.safe_message,
                status_code=last_rejection.status_code,
                retry_after=last_rejection.retry_after,
                route_attempts=tuple(attempts),
            ) from last_rejection
        raise AssertionError("routing policy unexpectedly had no routes")
