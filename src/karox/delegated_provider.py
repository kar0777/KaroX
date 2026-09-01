"""Provider wrapper that enforces a user-issued model delegation grant.

This module is deliberately below orchestration and above concrete transports.
It therefore works for a plain local agent, a ChatGPT-Web delegated worker, or a
future MCP client without teaching any of them how credentials are stored.

The wrapped provider MUST be created locally through :mod:`karox.provider_factory`.
The hosted caller supplies only a grant id and a task.  Pricing is resolved by a
trusted local :class:`ModelCallPricingResolver`; the grant ledger reserves the
worst-case request before the first network byte is sent.

Transport retries/fallbacks must not be hidden inside a grant-bound provider.
Callers should construct the concrete provider with transport retries disabled
and let a grant-aware outer policy decide whether another attempt is permitted.
That way every potentially billable attempt owns a distinct reservation.
"""

from __future__ import annotations

import dataclasses
from typing import Iterator, Mapping, Optional, Protocol

from .model_grants import (
    DelegationDenied,
    GrantScope,
    ModelGrantStore,
    PreparedModelCall,
)
from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    ModelResponse,
    Provider,
    accumulate_response,
)


class DelegatedProviderError(RuntimeError):
    """A local pricing/execution contract could not be satisfied safely."""


class ModelCallPricingResolver(Protocol):
    """Trusted local pricing boundary for one provider family.

    ``prepare`` returns a credential-free request description with conservative
    token/cost upper bounds and fresh pricing evidence. ``actual_cost_microusd``
    prices the provider's reported usage after the call. Returning ``None`` is
    allowed: the grant ledger then conservatively charges the preflight upper
    bound once network dispatch has begun.
    """

    def prepare(self, request: ModelRequest) -> PreparedModelCall: ...

    def actual_cost_microusd(
        self, call: PreparedModelCall, response: ModelResponse
    ) -> tuple[Optional[int], str]: ...


def _usage_int(usage: Mapping[str, object], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _response_usage(response: ModelResponse) -> tuple[int, int]:
    input_tokens = _usage_int(response.usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_int(response.usage, "completion_tokens", "output_tokens")
    return input_tokens, output_tokens


@dataclasses.dataclass(frozen=True)
class DelegationReceipt:
    """Content-free authorization receipt suitable for Mission Control."""

    grant_id: str
    reservation_id: str
    provider_id: str
    model_id: str
    spent_microusd: int
    input_tokens: int
    output_tokens: int
    cost_evidence: str
    breached: bool
    breach_reason: Optional[str]


class GrantEnforcedProvider:
    """A :class:`Provider` that cannot dispatch outside a user's grant.

    The wrapper has no credential accessor and never resolves the keyring.  Its
    ``underlying`` provider already owns that local-only closure.  The only
    authority that crosses from the hosted client is the opaque ``grant_id``;
    all scope, model, price, call, token and money checks are loaded locally.
    """

    def __init__(
        self,
        underlying: Provider,
        *,
        provider_id: str,
        grant_store: ModelGrantStore,
        grant_id: str,
        scope: GrantScope,
        pricing: ModelCallPricingResolver,
        idempotency_prefix: str,
    ) -> None:
        if not provider_id or any(char in provider_id for char in "\r\n\x00"):
            raise ValueError("delegated provider id must be short plain text")
        if not grant_id or any(char in grant_id for char in "\r\n\x00"):
            raise ValueError("delegation grant id must be short plain text")
        if not idempotency_prefix or len(idempotency_prefix) > 200:
            raise ValueError("delegated provider idempotency prefix is invalid")
        self.underlying = underlying
        self.provider_id = provider_id
        self.grant_store = grant_store
        self.grant_id = grant_id
        self.scope = scope
        self.pricing = pricing
        self.idempotency_prefix = idempotency_prefix
        self.last_receipt: Optional[DelegationReceipt] = None

    @property
    def provider_name(self) -> str:
        return f"grant:{self.provider_id}"

    def _prepare(self, request: ModelRequest) -> PreparedModelCall:
        call = self.pricing.prepare(request)
        if call.provider_id != self.provider_id:
            raise DelegatedProviderError("pricing resolver returned a different provider")
        if call.model_id != request.model:
            raise DelegatedProviderError("pricing resolver returned a different model")
        # The request itself is intentionally safe here: it contains prompt/tool
        # data but no provider credential. Concrete provider auth is a closure on
        # ``underlying`` and never enters the grant ledger.
        if call.payload is not request:
            raise DelegatedProviderError("pricing resolver must preserve the exact model request")
        return call

    def _idempotency_key(self, request: ModelRequest) -> str:
        # correlation_id is already stable for one ModelRequest and changes when
        # AgentKernel creates the next turn. The prefix pins it to the delegated
        # orchestration/host operation so unrelated sessions cannot collide.
        return f"{self.idempotency_prefix}:{request.correlation_id}"[:256]

    def _reserve(self, request: ModelRequest, call: PreparedModelCall):
        reservation = self.grant_store.reserve(
            self.grant_id,
            scope=self.scope,
            call=call,
            idempotency_key=self._idempotency_key(request),
        )
        if reservation.state == "settled":
            raise DelegationDenied(
                "already_settled",
                "delegated model request was already completed",
            )
        if reservation.network_started:
            raise DelegationDenied(
                "in_progress",
                "delegated model request is already in progress",
            )
        return reservation

    def _settle(
        self,
        call: PreparedModelCall,
        reservation_id: str,
        response: ModelResponse,
    ) -> DelegationReceipt:
        actual_cost, evidence = self.pricing.actual_cost_microusd(call, response)
        input_tokens, output_tokens = _response_usage(response)
        settlement = self.grant_store.settle(
            reservation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_microusd=actual_cost,
            cost_evidence=evidence,
        )
        receipt = DelegationReceipt(
            grant_id=self.grant_id,
            reservation_id=reservation_id,
            provider_id=call.provider_id,
            model_id=call.model_id,
            spent_microusd=settlement.spent_microusd,
            input_tokens=settlement.input_tokens,
            output_tokens=settlement.output_tokens,
            cost_evidence=settlement.cost_evidence,
            breached=settlement.breached,
            breach_reason=settlement.breach_reason,
        )
        self.last_receipt = receipt
        return receipt

    def complete(self, request: ModelRequest) -> ModelResponse:
        call = self._prepare(request)
        reservation = self._reserve(request, call)
        self.grant_store.mark_network_started(reservation.reservation_id)
        try:
            response = self.underlying.complete(request)
        except BaseException:
            self.grant_store.abort(reservation.reservation_id)
            raise
        self._settle(call, reservation.reservation_id, response)
        return response

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        call = self._prepare(request)
        reservation = self._reserve(request, call)
        self.grant_store.mark_network_started(reservation.reservation_id)
        collected: list[ModelEvent] = []
        completed = False
        settled = False
        try:
            for event in self.underlying.stream(request):
                collected.append(event)
                if event.kind is ModelEventKind.COMPLETION:
                    completed = True
                    # Hold the terminal event until accounting commits. Text and
                    # reasoning deltas remain streaming; the user never sees a
                    # logical completion whose grant still says "in progress".
                    continue
                yield event
            if not completed:
                raise DelegatedProviderError("provider stream ended without completion")
            response = accumulate_response(collected)
            self._settle(call, reservation.reservation_id, response)
            settled = True
            terminal = collected[-1]
            if terminal.kind is not ModelEventKind.COMPLETION:
                raise DelegatedProviderError("provider emitted data after terminal completion")
            yield terminal
        except BaseException:
            if not settled:
                self.grant_store.abort(reservation.reservation_id)
            raise
        finally:
            # GeneratorExit (consumer stops reading) is a real ambiguity once the
            # network request started. Idempotent abort makes this safe even if
            # the except path already handled the same reservation.
            if not settled:
                self.grant_store.abort(reservation.reservation_id)


__all__ = [
    "DelegatedProviderError",
    "DelegationReceipt",
    "GrantEnforcedProvider",
    "ModelCallPricingResolver",
]
