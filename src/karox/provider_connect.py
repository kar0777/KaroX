"""Connect a provider from the built-in catalog with nothing but its API key.

This is the one implementation behind ``karox provider connect`` and the TUI's
``/connect`` picker.  It turns a :mod:`karox.provider_catalog` entry into a
registry provider plus its models:

* the endpoint and wire protocol come from the catalog (or a verified preset);
* the key is checked against the provider's own model list when it publishes
  one, so a typo fails here instead of on the first agent turn;
* models are narrowed to the ones the account actually lists, and each keeps
  the catalog's context window, output limit and published price, so cost
  estimates and ``--max-cost`` work without hand-entered pricing;
* re-connecting never undoes hand-entered work: a model already in the
  registry keeps its own values and only gains the ones it lacked, and the
  provider keeps its headers, timeouts and Bypass preference.

Nothing here prints; callers own presentation.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional

from .credentials import CredentialStore
from .provider_catalog import (
    Catalog,
    CatalogError,
    CatalogProvider,
    catalog_model_records,
    plausible_output_limit,
    provider_catalog,
)
from .providers import ProviderError
from .registry import ModelPricing, ModelRecord, ProviderRecord, RegistryError

# Probe signature: (adapter_kind, base_url, model_id, credential, timeout) -> dict
Probe = Callable[..., dict[str, Any]]


class ConnectError(ValueError):
    """A connect request that cannot succeed; the message is safe to show."""


@dataclass(frozen=True)
class ConnectResult:
    provider_id: str
    catalog_provider: str
    display_name: str
    base_url: str
    model_id: str
    activated: bool
    models_installed: int
    priced_models: int
    snapshot: str
    # Models that were already in the registry and kept their own values.
    models_kept: int = 0
    verification: dict[str, Any] = field(default_factory=dict)
    mutation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.mutation,
            "provider_id": self.provider_id,
            "catalog_provider": self.catalog_provider,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "selected_model": self.model_id,
            "activated": self.activated,
            "models_installed": self.models_installed,
            "priced_models": self.priced_models,
            "models_kept": self.models_kept,
            "snapshot": self.snapshot,
            "verification": dict(self.verification),
        }


def _privacy_class(base_url: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    return "local" if host in {"localhost", "127.0.0.1", "::1"} else "public"


def catalog_entry_for(record: ProviderRecord, catalog: Optional[Catalog] = None) -> Optional[CatalogProvider]:
    """The catalog provider a registry provider corresponds to, by ID then endpoint."""
    catalog = catalog or provider_catalog()
    try:
        return catalog.provider(record.provider_id)
    except CatalogError:
        return catalog.provider_for_base_url(record.base_url)


def connected_catalog_ids(registry: Any, catalog: Optional[Catalog] = None) -> dict[str, str]:
    """Registry provider IDs keyed by the catalog provider they correspond to."""
    catalog = catalog or provider_catalog()
    connected: dict[str, str] = {}
    for record in registry.providers():
        entry = catalog_entry_for(record, catalog)
        if entry is not None:
            connected.setdefault(entry.provider_id, record.provider_id)
    return connected


def _discover(
    entry: CatalogProvider,
    provider: ProviderRecord,
    key: Optional[str],
    *,
    transport: Any = None,
) -> tuple[Optional[Any], dict[str, Any]]:
    from .model_discovery import ModelDiscoveryError, discover_models_result

    probe = replace(provider, credential_ref="os-keyring:provider/connect-probe" if key else None)
    try:
        found = discover_models_result(
            probe,
            (lambda _reference: key) if key else None,
            timeout_seconds=30.0,
            transport=transport,
        )
    except ModelDiscoveryError as exc:
        if exc.status_code in {401, 403}:
            raise ConnectError(
                f"{entry.display_name} rejected the API key (HTTP {exc.status_code})"
                + (f": {exc.detail}" if exc.detail else "")
            ) from None
        check: dict[str, Any] = {"status": "model_list_unavailable", "kind": exc.kind}
        if exc.status_code is not None:
            check["status_code"] = exc.status_code
        if exc.detail:
            check["detail"] = exc.detail
        return None, check
    return found, {"status": "ok", "listed_models": len(found.models)}


def _merged(discovered: ModelRecord, listed: ModelRecord) -> ModelRecord:
    """What the provider reports wins; the catalog fills only what it left out."""

    def capability(own: str, other: str) -> str:
        return own if own != "unknown" else other

    return replace(
        discovered,
        context_window=discovered.context_window or listed.context_window,
        max_output_tokens=discovered.max_output_tokens or listed.max_output_tokens,
        tools=capability(discovered.tools, listed.tools),
        vision=capability(discovered.vision, listed.vision),
        streaming=capability(discovered.streaming, listed.streaming),
        pricing=discovered.pricing or listed.pricing,
        display_name=discovered.display_name or listed.display_name,
        provenance=f"{discovered.provenance}+{listed.provenance}",
    )


def _kept(own: Optional[ModelRecord], offered: ModelRecord) -> ModelRecord:
    """A model already in the registry keeps what was entered for it.

    Re-connecting (a new key, a corrected endpoint) must not undo hand-entered
    work: aliases, limits, capabilities, prices and provenance stay, and the
    catalog or the provider's list only fills fields the record left empty --
    the same rule as ``catalog-sync``.
    """
    if own is None:
        return offered

    def capability(mine: str, other: str) -> str:
        return mine if mine != "unknown" else other

    context = own.context_window or offered.context_window
    return replace(
        own,
        aliases=own.aliases or offered.aliases,
        context_window=context,
        max_output_tokens=own.max_output_tokens
        or plausible_output_limit(context, offered.max_output_tokens),
        tools=capability(own.tools, offered.tools),
        vision=capability(own.vision, offered.vision),
        structured_output=capability(own.structured_output, offered.structured_output),
        streaming=capability(own.streaming, offered.streaming),
        pricing=own.pricing or offered.pricing,
        display_name=own.display_name or offered.display_name,
    )


def _installable_models(
    entry: CatalogProvider,
    provider: ProviderRecord,
    catalog: Catalog,
    found: Optional[Any],
    *,
    include_deprecated: bool,
) -> list[ModelRecord]:
    from .model_discovery import model_record

    records = catalog_model_records(
        entry, provider.provider_id, catalog, include_deprecated=include_deprecated
    )
    if found is None:
        return records
    by_id = {item.model_id: item for item in records}
    narrowed: list[ModelRecord] = []
    extra: list[ModelRecord] = []
    for item in found.models:
        try:
            discovered = model_record(provider.provider_id, item)
        except ValueError:
            continue
        listed = by_id.get(item.model_id)
        if listed is not None:
            narrowed.append(_merged(discovered, listed))
        elif discovered.tools == "true":
            # Not in the catalog but the provider says it can call tools.
            extra.append(discovered)
    if narrowed:
        return narrowed + extra
    # The account lists IDs the catalog does not use: keep both views.
    return records + extra


def _primary(
    records: list[ModelRecord],
    entry: CatalogProvider,
    wanted: str,
    selected: Optional[ModelRecord],
    provider_id: str,
) -> ModelRecord:
    by_id = {item.model_id: item for item in records}
    if wanted:
        match = by_id.get(wanted) or next(
            (item for item in records if item.model_id.lower() == wanted.lower()), None
        )
        if match is None:
            raise ConnectError(
                f"{wanted} is not offered by {entry.display_name}; "
                f"see: karox provider catalog --provider {entry.provider_id}"
            )
        return match
    if selected is not None and selected.provider_id == provider_id and selected.model_id in by_id:
        return by_id[selected.model_id]
    recommended = entry.recommended_model()
    if recommended is not None and recommended.model_id in by_id:
        return by_id[recommended.model_id]
    return records[0]


def connect_from_catalog(
    controller: Any,
    catalog_id: str,
    *,
    secret: Optional[str] = None,
    credential_ref: Optional[str] = None,
    provider_id: Optional[str] = None,
    base_url: Optional[str] = None,
    variables: Optional[Mapping[str, str]] = None,
    model: Optional[str] = None,
    include_deprecated: bool = False,
    test: bool = True,
    activate: Optional[bool] = None,
    catalog: Optional[Catalog] = None,
    probe: Optional[Probe] = None,
    transport: Any = None,
) -> ConnectResult:
    """Install a catalog provider; ``activate=None`` selects it only when sensible.

    Exactly one of ``secret`` (stored in the OS keyring by the controller) or
    ``credential_ref`` (``env:NAME`` / ``os-keyring:NAME``) may be given; with
    neither, an existing registry entry keeps its credential.
    """

    if secret is not None and credential_ref:
        raise ConnectError("pass either an API key or a credential reference, not both")
    catalog = catalog or provider_catalog()
    try:
        entry = catalog.provider(catalog_id)
    except CatalogError as exc:
        raise ConnectError(str(exc)) from None
    if not entry.installable and not (base_url and entry.adapter_kind):
        raise ConnectError(
            f"{entry.display_name} cannot be connected with an API key: "
            f"{entry.unsupported_reason or 'no documented endpoint'}"
        )
    try:
        endpoint = (base_url or entry.resolved_base_url(variables or {})).strip().rstrip("/")
    except CatalogError as exc:
        raise ConnectError(str(exc)) from None
    if not endpoint.lower().startswith(("https://", "http://")):
        raise ConnectError("the endpoint must be an http(s) URL")
    registry_id = (provider_id or entry.provider_id).strip()
    existing: Optional[ProviderRecord] = None
    with contextlib.suppress(RegistryError, ValueError):
        existing = controller.registry.provider(registry_id)
    reference = credential_ref or (existing.credential_ref if existing and secret is None else None)
    if secret is not None and not secret.strip():
        raise ConnectError("no API key entered")
    if secret is None and not reference and entry.env:
        raise ConnectError(
            f"{entry.display_name} needs an API key (usually in {', '.join(entry.env)})"
        )
    key = secret.strip() if secret is not None else None
    if key is None and reference and test:
        try:
            key = CredentialStore().resolve(reference)
        except Exception as exc:  # noqa: BLE001 - never echo backend detail
            raise ConnectError(
                f"the credential {reference} could not be read ({type(exc).__name__})"
            ) from None

    provider = ProviderRecord(
        provider_id=registry_id,
        adapter_kind=str(entry.adapter_kind),
        base_url=endpoint,
        credential_ref=None if key is not None and secret is not None else reference,
        privacy_class=_privacy_class(endpoint),
    )
    if existing is not None:
        # Re-connecting keeps what the catalog knows nothing about: extra
        # headers and query parameters, timeouts, the Bypass preference, and a
        # privacy class chosen for this same endpoint.
        same_endpoint = existing.base_url.rstrip("/") == endpoint
        provider = replace(
            existing,
            adapter_kind=provider.adapter_kind,
            base_url=provider.base_url,
            credential_ref=provider.credential_ref,
            privacy_class=existing.privacy_class if same_endpoint else provider.privacy_class,
        )
    found, check = (None, {"status": "skipped"})
    if test:
        found, check = _discover(entry, provider, key, transport=transport)
        effective = str(getattr(found, "base_url", "") or "").rstrip("/") if found else ""
        if effective and effective != endpoint:
            provider = replace(provider, base_url=effective)
            check["base_url_corrected"] = True
    records = _installable_models(
        entry, provider, catalog, found, include_deprecated=include_deprecated
    )
    if not records:
        raise ConnectError(f"{entry.display_name} lists no tool-capable models")
    entered = (
        {item.model_id: item for item in controller.registry.models(registry_id)}
        if existing is not None
        else {}
    )
    records = [_kept(entered.get(item.model_id), item) for item in records]
    selected = controller.registry.selected_model()
    primary = _primary(records, entry, (model or "").strip(), selected, registry_id)
    if activate is None:
        activate = bool((model or "").strip()) or selected is None or selected.provider_id == registry_id
    if test and found is None and key is not None and probe is not None:
        # No model list to check the key against: one tiny completion does it.
        try:
            check["probe"] = probe(
                str(entry.adapter_kind), provider.base_url, primary.model_id,
                credential=lambda: key, timeout_seconds=60.0,
            )
        except ProviderError as exc:
            status = f" (HTTP {exc.status_code})" if exc.status_code else ""
            raise ConnectError(
                f"{entry.display_name} refused a test request{status}: {exc.safe_message}"
            ) from None
    mutation = controller.configure_provider_model(
        provider, primary, secret=key if secret is not None else None, activate=activate
    )
    controller.registry.put_models(item for item in records if item.model_id != primary.model_id)
    return ConnectResult(
        provider_id=registry_id,
        catalog_provider=entry.provider_id,
        display_name=entry.display_name,
        base_url=provider.base_url,
        model_id=primary.model_id,
        activated=bool(activate),
        models_installed=len(records),
        priced_models=sum(1 for item in records if item.pricing is not None),
        snapshot=catalog.fetched_at,
        models_kept=sum(1 for item in records if item.model_id in entered),
        verification=check,
        mutation=mutation.to_dict(),
    )


def _catalog_pricing(match: Any, catalog: Catalog) -> ModelPricing:
    return ModelPricing(
        version=catalog.pricing_version,
        currency="USD",
        input_per_million=float(match.input_per_million or 0.0),
        output_per_million=float(match.output_per_million or 0.0),
        source=catalog.pricing_source,
        cache_read_per_million=match.cache_read_per_million,
        cache_write_per_million=match.cache_write_per_million,
    )


def sync_from_catalog(
    registry: Any,
    provider_ids: Iterable[str] = (),
    *,
    catalog: Optional[Catalog] = None,
) -> dict[str, Any]:
    """Fill prices and limits the registry lacks; never overwrite a user value."""
    catalog = catalog or provider_catalog()
    wanted = set(provider_ids or ())
    updated: list[ModelRecord] = []
    report: list[dict[str, Any]] = []
    for record in registry.providers():
        if wanted and record.provider_id not in wanted:
            continue
        entry = catalog_entry_for(record, catalog)
        if entry is None:
            report.append({"provider_id": record.provider_id, "status": "not_in_catalog"})
            continue
        filled = 0
        for model in registry.models(record.provider_id):
            match = entry.model(model.model_id)
            if match is None:
                continue
            changes: dict[str, Any] = {}
            if model.pricing is None and match.priced:
                changes["pricing"] = _catalog_pricing(match, catalog)
            if model.context_window is None and match.context_window:
                changes["context_window"] = match.context_window
            limit = plausible_output_limit(
                model.context_window or match.context_window, match.max_output_tokens
            )
            if model.max_output_tokens is None and limit:
                changes["max_output_tokens"] = limit
            if changes:
                updated.append(replace(model, **changes))
                filled += 1
        report.append(
            {
                "provider_id": record.provider_id,
                "catalog_provider": entry.provider_id,
                "models_updated": filled,
            }
        )
    registry.put_models(updated)
    return {"snapshot": catalog.fetched_at, "models_updated": len(updated), "providers": report}


__all__ = [
    "ConnectError",
    "ConnectResult",
    "catalog_entry_for",
    "connect_from_catalog",
    "connected_catalog_ids",
    "sync_from_catalog",
]
