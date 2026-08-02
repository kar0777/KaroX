"""Unified orchestration for providers, credentials, models, and selection.

The registry deliberately stores only opaque credential references.  A useful
management surface therefore has to coordinate two independent durable stores:
provider/model JSON and the operating-system credential backend.  Keeping that
coordination in CLI and TUI handlers made edit/remove semantics drift and left
selected-model repair to chance.

``ProviderController`` is the single mutation boundary.  It never returns a raw
secret, cleans up only unshared keyring credentials, and repairs a missing
selection deterministically after destructive model/provider operations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Optional, Protocol

from .credentials import (
    ENVIRONMENT_SCHEME,
    KEYRING_SCHEME,
    CredentialError,
    CredentialReference,
    CredentialStore,
)
from .provider_factory import ProviderFactory
from .providers import ModelMessage, ModelRequest
from .registry import ModelRecord, ProviderRecord, ProviderRegistry, RegistryError


class ProviderTester(Protocol):
    def __call__(
        self,
        provider: ProviderRecord,
        model: ModelRecord,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProviderDetails:
    provider: ProviderRecord
    models: tuple[ModelRecord, ...]
    selected_model: Optional[ModelRecord]
    credential: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        selected = self.selected_model
        return {
            "provider": asdict(self.provider),
            "models": [asdict(item) for item in self.models],
            "selected_model": asdict(selected) if selected is not None else None,
            "credential": dict(self.credential),
        }


@dataclass(frozen=True)
class ProviderMutation:
    status: str
    provider: Optional[ProviderRecord] = None
    model: Optional[ModelRecord] = None
    selected_model: Optional[ModelRecord] = None
    credential_cleanup: str = "not_requested"
    credential_fingerprint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": asdict(self.provider) if self.provider is not None else None,
            "model": asdict(self.model) if self.model is not None else None,
            "selected_model": (
                asdict(self.selected_model)
                if self.selected_model is not None
                else None
            ),
            "credential_cleanup": self.credential_cleanup,
            "credential_fingerprint": self.credential_fingerprint or "",
        }


class ProviderController:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        credentials: Optional[CredentialStore] = None,
        tester: Optional[ProviderTester] = None,
    ) -> None:
        self.registry = registry
        self.credentials = credentials or CredentialStore()
        self._tester = tester or self._default_test

    def _credential_status(self, provider: ProviderRecord) -> dict[str, Any]:
        reference = provider.credential_ref
        if not reference:
            return {
                "configured": False,
                "available": False,
                "reference": "",
                "scheme": "none",
                "fingerprint": "",
            }
        parsed = CredentialReference.parse(reference)
        try:
            secret = self.credentials.resolve(parsed)
        except CredentialError as exc:
            return {
                "configured": True,
                "available": False,
                "reference": reference,
                "scheme": parsed.scheme,
                "fingerprint": "",
                "error": str(exc),
            }
        return {
            "configured": True,
            "available": True,
            "reference": reference,
            "scheme": parsed.scheme,
            "fingerprint": self.credentials.fingerprint(secret),
        }

    def details(self, provider_id: str) -> ProviderDetails:
        provider = self.registry.provider(provider_id)
        models = tuple(self.registry.models(provider_id))
        selected = self.registry.selected_model()
        if selected is not None and selected.provider_id != provider_id:
            selected = None
        return ProviderDetails(
            provider=provider,
            models=models,
            selected_model=selected,
            credential=self._credential_status(provider),
        )

    def list(self) -> list[ProviderDetails]:
        return [self.details(item.provider_id) for item in self.registry.providers()]

    def put_provider(self, record: ProviderRecord) -> ProviderMutation:
        return ProviderMutation(
            status="saved",
            provider=self.registry.put_provider(record),
            selected_model=self.registry.selected_model(),
        )

    def edit_provider(
        self,
        provider_id: str,
        *,
        adapter_kind: Optional[str] = None,
        base_url: Optional[str] = None,
        credential_ref: Optional[str] = None,
        credential_ref_supplied: bool = False,
        headers: Optional[dict[str, str]] = None,
        query: Optional[dict[str, str]] = None,
        privacy_class: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_transport_retries: Optional[int] = None,
    ) -> ProviderMutation:
        current = self.registry.provider(provider_id)
        updated = replace(
            current,
            adapter_kind=adapter_kind or current.adapter_kind,
            base_url=base_url or current.base_url,
            credential_ref=(
                credential_ref if credential_ref_supplied else current.credential_ref
            ),
            headers=dict(headers) if headers is not None else current.headers,
            query=dict(query) if query is not None else current.query,
            privacy_class=privacy_class or current.privacy_class,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else current.timeout_seconds
            ),
            max_transport_retries=(
                max_transport_retries
                if max_transport_retries is not None
                else current.max_transport_retries
            ),
        )
        return ProviderMutation(
            status="updated",
            provider=self.registry.put_provider(updated),
            selected_model=self.registry.selected_model(),
        )

    def _reference_is_shared(self, reference: str, *, excluding: str) -> bool:
        return any(
            item.provider_id != excluding and item.credential_ref == reference
            for item in self.registry.providers()
        )

    def _delete_reference_if_owned(
        self,
        reference: Optional[str],
        *,
        excluding: str,
    ) -> str:
        if not reference:
            return "not_configured"
        parsed = CredentialReference.parse(reference)
        if parsed.scheme == ENVIRONMENT_SCHEME:
            return "environment_not_deleted"
        if parsed.scheme != KEYRING_SCHEME:
            return "unsupported_scheme"
        if self._reference_is_shared(reference, excluding=excluding):
            return "shared_not_deleted"
        try:
            self.credentials.delete(parsed.name)
        except CredentialError:
            return "delete_failed"
        return "deleted"

    def set_credential(
        self,
        provider_id: str,
        secret: str,
        *,
        credential_name: Optional[str] = None,
    ) -> ProviderMutation:
        current = self.registry.provider(provider_id)
        previous_ref = current.credential_ref
        previous_secret: Optional[str] = None
        previous_parsed: Optional[CredentialReference] = None
        if previous_ref:
            previous_parsed = CredentialReference.parse(previous_ref)
            if previous_parsed.scheme == KEYRING_SCHEME:
                try:
                    previous_secret = self.credentials.resolve(previous_parsed)
                except CredentialError:
                    previous_secret = None

        name = credential_name
        if name is None and previous_parsed is not None and previous_parsed.scheme == KEYRING_SCHEME:
            name = previous_parsed.name
        name = name or provider_id
        stored = self.credentials.set(name, secret)
        new_ref = stored["reference"]
        try:
            updated = replace(current, credential_ref=new_ref)
            self.registry.put_provider(updated)
        except Exception:
            # Restore an overwritten prior value when possible.  A newly-created
            # credential is deleted so a registry failure never leaves an orphan.
            if previous_parsed is not None and previous_parsed.name == name and previous_secret is not None:
                self.credentials.set(name, previous_secret)
            else:
                try:
                    self.credentials.delete(name)
                except CredentialError:
                    pass
            raise

        cleanup = "not_requested"
        if previous_ref and previous_ref != new_ref:
            cleanup = self._delete_reference_if_owned(
                previous_ref,
                excluding=provider_id,
            )
        return ProviderMutation(
            status="credential_saved",
            provider=updated,
            selected_model=self.registry.selected_model(),
            credential_cleanup=cleanup,
            credential_fingerprint=stored["fingerprint"],
        )

    def clear_credential(
        self,
        provider_id: str,
        *,
        delete_stored: bool = False,
    ) -> ProviderMutation:
        current = self.registry.provider(provider_id)
        previous_ref = current.credential_ref
        updated = replace(current, credential_ref=None)
        self.registry.put_provider(updated)
        cleanup = (
            self._delete_reference_if_owned(previous_ref, excluding=provider_id)
            if delete_stored
            else "not_requested"
        )
        return ProviderMutation(
            status="credential_cleared",
            provider=updated,
            selected_model=self.registry.selected_model(),
            credential_cleanup=cleanup,
        )

    def configure_provider_model(
        self,
        provider: ProviderRecord,
        model: ModelRecord,
        *,
        secret: Optional[str] = None,
        credential_name: Optional[str] = None,
        activate: bool = True,
    ) -> ProviderMutation:
        """Persist provider + credential + model as one recoverable operation.

        The registry writes atomically, but it and the OS keyring are independent
        stores.  This method snapshots the old logical state and restores it if a
        later step fails, so an interactive setup never leaves a new key without
        its provider or a provider without the model the user just verified.
        """

        try:
            previous_provider = self.registry.provider(provider.provider_id)
        except RegistryError:
            previous_provider = None
        previous_models = tuple(self.registry.models(provider.provider_id))
        previous_selected = self.registry.selected_model()

        previous_reference = (
            previous_provider.credential_ref if previous_provider is not None else None
        )
        previous_secret: Optional[str] = None
        previous_keyring_name: Optional[str] = None
        if previous_reference:
            parsed = CredentialReference.parse(previous_reference)
            if parsed.scheme == KEYRING_SCHEME:
                previous_keyring_name = parsed.name
                try:
                    previous_secret = self.credentials.resolve(parsed)
                except CredentialError:
                    previous_secret = None

        new_keyring_name: Optional[str] = None
        try:
            base = provider
            if secret is None and previous_provider is not None and not base.credential_ref:
                base = replace(base, credential_ref=previous_provider.credential_ref)
            saved_provider = self.registry.put_provider(base)
            if secret is not None:
                credential_result = self.set_credential(
                    provider.provider_id,
                    secret,
                    credential_name=credential_name,
                )
                assert credential_result.provider is not None
                saved_provider = credential_result.provider
                parsed_new = CredentialReference.parse(saved_provider.credential_ref or "")
                if parsed_new.scheme == KEYRING_SCHEME:
                    new_keyring_name = parsed_new.name
            saved_model = self.registry.put_model(model)
            selected = (
                self.registry.select_model(saved_model.provider_id, saved_model.model_id)
                if activate
                else self.registry.selected_model()
            )
            return ProviderMutation(
                status="configured",
                provider=saved_provider,
                model=saved_model,
                selected_model=selected,
                credential_fingerprint=(
                    self._credential_status(saved_provider).get("fingerprint") or None
                ),
            )
        except Exception:
            # Restore the provider/model JSON first.  Every individual registry
            # write is atomic, and this sequence is deterministic and bounded.
            try:
                self.registry.remove_provider(provider.provider_id, cascade=True)
            except RegistryError:
                pass
            if previous_provider is not None:
                self.registry.put_provider(previous_provider)
                for previous_model in previous_models:
                    self.registry.put_model(previous_model)
            if previous_selected is not None:
                try:
                    self.registry.select_model(
                        previous_selected.provider_id,
                        previous_selected.model_id,
                    )
                except RegistryError:
                    pass

            # Restore or remove the keyring value written by this operation.
            if (
                previous_keyring_name is not None
                and previous_secret is not None
            ):
                # ``set_credential`` may have deleted the previous unshared
                # reference after successfully switching the provider to a new
                # account. A later model failure must recreate that old value,
                # regardless of whether the account name stayed the same.
                self.credentials.set(previous_keyring_name, previous_secret)
            if (
                new_keyring_name is not None
                and new_keyring_name != previous_keyring_name
            ):
                try:
                    self.credentials.delete(new_keyring_name)
                except CredentialError:
                    pass
            raise

    def put_model(self, record: ModelRecord) -> ProviderMutation:
        saved = self.registry.put_model(record)
        return ProviderMutation(
            status="saved",
            model=saved,
            selected_model=self.registry.selected_model(),
        )

    def map_alias(self, provider_id: str, alias: str, model_id: str) -> ProviderMutation:
        model = self.registry.map_alias(provider_id, alias, model_id)
        return ProviderMutation(
            status="mapped",
            model=model,
            selected_model=self.registry.selected_model(),
        )

    def select_model(self, provider_id: str, model_or_alias: str) -> ProviderMutation:
        selected = self.registry.select_model(provider_id, model_or_alias)
        return ProviderMutation(status="selected", model=selected, selected_model=selected)

    def repair_selection(
        self,
        *,
        preferred_provider: Optional[str] = None,
    ) -> Optional[ModelRecord]:
        selected = self.registry.selected_model()
        if selected is not None:
            return selected
        models = self.registry.models(preferred_provider)
        if not models and preferred_provider is not None:
            models = self.registry.models()
        if not models:
            return None
        first = sorted(models, key=lambda item: (item.provider_id, item.model_id))[0]
        return self.registry.select_model(first.provider_id, first.model_id)

    def remove_model(
        self,
        provider_id: str,
        model_or_alias: str,
        *,
        repair_selection: bool = True,
    ) -> ProviderMutation:
        removed = self.registry.remove_model(provider_id, model_or_alias)
        selected = (
            self.repair_selection(preferred_provider=provider_id)
            if repair_selection
            else self.registry.selected_model()
        )
        return ProviderMutation(
            status="removed",
            model=removed,
            selected_model=selected,
        )

    def remove_provider(
        self,
        provider_id: str,
        *,
        cascade: bool = False,
        delete_credential: bool = False,
        repair_selection: bool = True,
    ) -> ProviderMutation:
        current = self.registry.provider(provider_id)
        removed = self.registry.remove_provider(provider_id, cascade=cascade)
        cleanup = (
            self._delete_reference_if_owned(
                current.credential_ref,
                excluding=provider_id,
            )
            if delete_credential
            else "not_requested"
        )
        selected = self.repair_selection() if repair_selection else self.registry.selected_model()
        return ProviderMutation(
            status="removed",
            provider=removed,
            selected_model=selected,
            credential_cleanup=cleanup,
        )

    def test_provider(
        self,
        provider_id: str,
        *,
        model_or_alias: Optional[str] = None,
    ) -> dict[str, Any]:
        provider = self.registry.provider(provider_id)
        if model_or_alias is None:
            models = self.registry.models(provider_id)
            if not models:
                raise RegistryError(f"provider has no registered models: {provider_id}")
            model = models[0]
        else:
            model = self.registry.model(provider_id, model_or_alias)
        return self._tester(provider, model)

    @staticmethod
    def _default_test(
        provider: ProviderRecord,
        model: ModelRecord,
    ) -> dict[str, Any]:
        response = ProviderFactory().create(provider).complete(
            ModelRequest(
                model=model.model_id,
                messages=(ModelMessage("user", "Reply with exactly OK."),),
                max_output_tokens=8,
                deadline_seconds=min(60.0, provider.timeout_seconds),
            )
        )
        return {
            "status": "ok",
            "provider_id": provider.provider_id,
            "model_id": model.model_id,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "transport_attempts": response.transport_attempts,
        }


__all__ = [
    "ProviderController",
    "ProviderDetails",
    "ProviderMutation",
]
