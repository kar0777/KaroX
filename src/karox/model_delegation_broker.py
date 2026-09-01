"""Provider-neutral broker for user-authorized delegated model calls.

The broker is the only surface a hosted client needs: provider/model + request +
opaque grant id.  It validates the local provider/model registry, chooses one
trusted delegation adapter, and returns content plus a content-free spend
receipt.  API keys remain behind :class:`karox.credentials.CredentialStore`.

Adding a provider does not add a new permission system.  A provider integration
implements :class:`DelegationAdapter`; grants, reservations, scopes, expiry,
revocation and audit semantics stay identical for every connection type.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Protocol, Sequence
from urllib.parse import urlsplit

import httpx

from .credentials import CredentialStore
from .delegated_provider import DelegationReceipt, GrantEnforcedProvider
from .model_grants import GrantScope, GrantUsage, ModelGrantStore
from .openrouter_delegation import (
    OpenRouterPriceCappedChatProvider,
    OpenRouterPricingResolver,
)
from .providers import ModelRequest, ModelResponse
from .registry import ModelRecord, ProviderRecord, ProviderRegistry, RegistryError


class ModelDelegationBrokerError(RuntimeError):
    pass


class DelegationAdapter(Protocol):
    """Local provider-family integration. No method may return a credential."""

    def supports(self, provider: ProviderRecord) -> bool: ...

    def build(
        self,
        *,
        provider: ProviderRecord,
        model: ModelRecord,
        credentials: CredentialStore,
        grant_store: ModelGrantStore,
        grant_id: str,
        scope: GrantScope,
        operation_id: str,
    ) -> GrantEnforcedProvider: ...


@dataclasses.dataclass(frozen=True)
class DelegatedCallOutcome:
    provider_id: str
    model_id: str
    grant_id: str
    response: ModelResponse
    receipt: DelegationReceipt
    grant_usage: GrantUsage


class OpenRouterDelegationAdapter:
    """OpenRouter Chat Completions adapter with local keyring + server price cap."""

    def __init__(
        self,
        *,
        pricing_cache_seconds: float = 120.0,
        pricing_timeout_seconds: float = 10.0,
        clock: Callable[[], float],
        pricing_client_factory: Callable[[], httpx.Client] = httpx.Client,
    ) -> None:
        self.pricing_cache_seconds = pricing_cache_seconds
        self.pricing_timeout_seconds = pricing_timeout_seconds
        self.clock = clock
        self.pricing_client_factory = pricing_client_factory

    def supports(self, provider: ProviderRecord) -> bool:
        host = (urlsplit(provider.base_url).hostname or "").lower()
        return bool(
            provider.adapter_kind == "openai_compatible_chat"
            and (host == "openrouter.ai" or host.endswith(".openrouter.ai"))
        )

    def build(
        self,
        *,
        provider: ProviderRecord,
        model: ModelRecord,
        credentials: CredentialStore,
        grant_store: ModelGrantStore,
        grant_id: str,
        scope: GrantScope,
        operation_id: str,
    ) -> GrantEnforcedProvider:
        if not self.supports(provider):
            raise ModelDelegationBrokerError("OpenRouter adapter received a different provider")
        if provider.credential_ref is None:
            raise ModelDelegationBrokerError("OpenRouter provider has no local credential reference")
        credential = credentials.accessor(provider.credential_ref)
        pricing = OpenRouterPricingResolver(
            base_url=provider.base_url,
            credential=credential,
            cache_seconds=self.pricing_cache_seconds,
            timeout_seconds=min(self.pricing_timeout_seconds, provider.timeout_seconds),
            clock=self.clock,
            client_factory=self.pricing_client_factory,
        )
        transport = OpenRouterPriceCappedChatProvider(
            provider.base_url,
            credential=credential,
            headers=provider.headers,
            query=provider.query,
            timeout_seconds=provider.timeout_seconds,
            pricing_resolver=pricing,
            # Constructor enforces zero hidden retries even if this value changes.
            max_transport_retries=0,
        )
        return GrantEnforcedProvider(
            transport,
            provider_id=provider.provider_id,
            grant_store=grant_store,
            grant_id=grant_id,
            scope=scope,
            pricing=pricing,
            idempotency_prefix=operation_id,
        )


class ModelDelegationBroker:
    """Fail-closed provider/model dispatch under a pre-issued user grant."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        credentials: CredentialStore,
        grant_store: ModelGrantStore,
        adapters: Sequence[DelegationAdapter],
    ) -> None:
        if not adapters:
            raise ValueError("model delegation broker requires at least one adapter")
        self.registry = registry
        self.credentials = credentials
        self.grant_store = grant_store
        self.adapters = tuple(adapters)

    def complete(
        self,
        *,
        provider_id: str,
        request: ModelRequest,
        grant_id: str,
        scope: GrantScope,
        operation_id: str,
    ) -> DelegatedCallOutcome:
        try:
            provider = self.registry.provider(provider_id)
            model = self.registry.model(provider_id, request.model)
        except RegistryError as exc:
            raise ModelDelegationBrokerError(str(exc)) from exc
        if not provider.enabled:
            raise ModelDelegationBrokerError("provider is disabled")
        if model.provider_id != provider.provider_id:
            raise ModelDelegationBrokerError("model/provider registry mismatch")
        # Resolve aliases before grant matching/pricing. The provider and the
        # ledger must authorize the same canonical model identifier.
        canonical_request = (
            request
            if request.model == model.model_id
            else dataclasses.replace(request, model=model.model_id)
        )
        matches = [adapter for adapter in self.adapters if adapter.supports(provider)]
        if not matches:
            raise ModelDelegationBrokerError(
                "provider has no delegation adapter with trustworthy pricing"
            )
        if len(matches) > 1:
            raise ModelDelegationBrokerError(
                "provider matches multiple delegation adapters; refusing ambiguous authority"
            )
        delegated = matches[0].build(
            provider=provider,
            model=model,
            credentials=self.credentials,
            grant_store=self.grant_store,
            grant_id=grant_id,
            scope=scope,
            operation_id=operation_id,
        )
        response = delegated.complete(canonical_request)
        receipt = delegated.last_receipt
        if receipt is None:
            raise ModelDelegationBrokerError("delegated provider returned without a spend receipt")
        return DelegatedCallOutcome(
            provider_id=provider.provider_id,
            model_id=model.model_id,
            grant_id=grant_id,
            response=response,
            receipt=receipt,
            grant_usage=self.grant_store.status(grant_id),
        )


__all__ = [
    "DelegatedCallOutcome",
    "DelegationAdapter",
    "ModelDelegationBroker",
    "ModelDelegationBrokerError",
    "OpenRouterDelegationAdapter",
]
