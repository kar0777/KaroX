"""Explicit provider routing with fail-closed capabilities and hard budgets."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterator, Mapping, Protocol

from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ToolCallDelta,
)
from .registry import ModelRecord, ProviderRecord, ProviderRegistry


_PRIVACY_RANK = {"local": 0, "private": 1, "public": 2}
_FALLBACK_ERRORS = frozenset(
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
class RoutingPolicy:
    routes: tuple[RouteTarget, ...]
    privacy_limit: str = "public"
    max_total_tokens: int | None = None
    max_cost: float | None = None
    currency: str | None = None
    require_tools: bool = True
    require_streaming: bool = True

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("routing requires at least one explicit route")
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
    """Try only declared routes and expose a durable, secret-free audit trail."""

    provider_name = "routed_provider"

    def __init__(
        self,
        registry: ProviderRegistry,
        factory: ProviderBuilder | Callable[[ProviderRecord], Provider],
        policy: RoutingPolicy,
        *,
        initial_usage: Mapping[str, Any] | None = None,
        initial_costs: Mapping[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.factory = factory
        self.policy = policy
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

    @staticmethod
    def _attempt(
        index: int,
        target: RouteTarget,
        *,
        model_id: str | None = None,
        status: str,
        error: ProviderError | None = None,
    ) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "route_index": index,
            "provider_id": target.provider_id,
            "model": model_id or target.model,
            "status": status,
        }
        if error is not None:
            value["error_kind"] = error.kind.value
            value["message"] = error.safe_message
            value["status_code"] = error.status_code
        return value

    def complete(self, request: ModelRequest) -> ModelResponse:
        self._check_global_budgets()
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

            # The registry records each model's output ceiling; routing used to
            # rewrite only the model id, so the ceiling was never sent. The
            # Anthropic adapter then fell back to its own 4096-token default and
            # truncated long work mid-answer. An explicit request value still
            # wins, because the caller is closer to the task than the registry.
            routed_request = replace(
                request,
                model=model_record.model_id,
                max_output_tokens=(
                    request.max_output_tokens
                    if request.max_output_tokens is not None
                    else model_record.max_output_tokens
                ),
            )
            try:
                response = self._provider(provider_record).complete(routed_request)
            except ProviderError as exc:
                can_fallback = (
                    exc.kind in _FALLBACK_ERRORS
                    and index + 1 < len(self.policy.routes)
                )
                attempts.append(
                    self._attempt(
                        index,
                        target,
                        model_id=model_record.model_id,
                        status="fallback" if can_fallback else "failed",
                        error=exc,
                    )
                )
                if can_fallback:
                    continue
                raise ProviderError(
                    exc.kind,
                    exc.safe_message,
                    status_code=exc.status_code,
                    retry_after=exc.retry_after,
                    route_attempts=tuple(attempts),
                ) from exc

            attempts.append(
                self._attempt(
                    index,
                    target,
                    model_id=model_record.model_id,
                    status="completed",
                )
            )
            self._usage = _merge_usage(self._usage, response.usage)
            cost = None
            currency = None
            pricing_version = None
            cumulative_cost = None
            if model_record.pricing is not None:
                cost = model_record.pricing.estimate(response.usage)
                currency = model_record.pricing.currency
                pricing_version = model_record.pricing.version
                self._costs[currency] = round(
                    self._costs.get(currency, 0.0) + cost, 12
                )
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
        if last_rejection is not None:
            raise ProviderError(
                last_rejection.kind,
                last_rejection.safe_message,
                status_code=last_rejection.status_code,
                retry_after=last_rejection.retry_after,
                route_attempts=tuple(attempts),
            ) from last_rejection
        raise AssertionError("routing policy unexpectedly had no routes")

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        """Expose normalized events while preserving routing/budget semantics.

        The router must inspect final usage before tool calls become actionable,
        so it completes one routed response and then emits normalized events.
        Concrete adapters still consume their upstream response as a stream.
        """

        response = self.complete(request)
        if response.content is not None:
            yield ModelEvent(
                ModelEventKind.TEXT_DELTA,
                text_delta=response.content,
                response_id=response.response_id,
                transport_attempts=response.transport_attempts,
            )
        for index, call in enumerate(response.tool_calls):
            yield ModelEvent(
                ModelEventKind.TOOL_CALL_DELTA,
                tool_call_delta=ToolCallDelta(
                    index=index,
                    call_id_fragment=call.call_id,
                    name_fragment=call.name,
                    arguments_fragment=call.raw_arguments,
                ),
                response_id=response.response_id,
                transport_attempts=response.transport_attempts,
            )
        if response.usage:
            yield ModelEvent(
                ModelEventKind.USAGE,
                usage=response.usage,
                response_id=response.response_id,
                transport_attempts=response.transport_attempts,
            )
        yield ModelEvent(
            ModelEventKind.COMPLETION,
            finish_reason=response.finish_reason,
            response_id=response.response_id,
            transport_attempts=response.transport_attempts,
        )
