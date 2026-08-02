"""Contract tests for unified provider/model/credential orchestration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401

from karox.credentials import CredentialStore
from karox.provider_controller import ProviderController
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry, RegistryError


class _MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.deleted: list[tuple[str, str]] = []

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.values:
            from karox.credentials import CredentialError

            raise CredentialError("credential does not exist")
        self.deleted.append((service, account))
        del self.values[(service, account)]


class ProviderControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = ProviderRegistry(self.root / "providers.json")
        self.backend = _MemoryCredentials()
        self.credentials = CredentialStore(self.backend)
        self.controller = ProviderController(
            registry=self.registry,
            credentials=self.credentials,
            tester=lambda provider, model: {
                "status": "ok",
                "provider_id": provider.provider_id,
                "model_id": model.model_id,
            },
        )
        self.registry.put_provider(
            ProviderRecord(
                provider_id="alpha",
                adapter_kind="openai_compatible_chat",
                base_url="https://alpha.example/v1",
                privacy_class="private",
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_details_groups_models_selection_and_secret_free_credential_status(self) -> None:
        mutation = self.controller.set_credential("alpha", "secret-alpha")
        self.registry.put_model(ModelRecord("alpha", "model-b"))
        self.registry.put_model(ModelRecord("alpha", "model-a", aliases=("default",)))
        self.controller.select_model("alpha", "default")

        details = self.controller.details("alpha").to_dict()
        self.assertEqual(
            [item["model_id"] for item in details["models"]],
            ["model-a", "model-b"],
        )
        self.assertEqual(details["selected_model"]["model_id"], "model-a")
        self.assertTrue(details["credential"]["available"])
        self.assertEqual(
            details["credential"]["fingerprint"],
            mutation.credential_fingerprint,
        )
        self.assertNotIn("secret-alpha", repr(details))
        self.assertEqual(
            self.controller.test_provider("alpha", model_or_alias="default")["model_id"],
            "model-a",
        )

    def test_edit_preserves_unspecified_fields_and_can_clear_reference(self) -> None:
        self.controller.set_credential("alpha", "secret-alpha")
        updated = self.controller.edit_provider(
            "alpha",
            base_url="https://new-alpha.example/v1",
            timeout_seconds=120,
        ).provider
        assert updated is not None
        self.assertEqual(updated.adapter_kind, "openai_compatible_chat")
        self.assertEqual(updated.privacy_class, "private")
        self.assertIsNotNone(updated.credential_ref)
        self.assertEqual(updated.timeout_seconds, 120)

        cleared = self.controller.clear_credential(
            "alpha", delete_stored=True
        )
        assert cleared.provider is not None
        self.assertIsNone(cleared.provider.credential_ref)
        self.assertEqual(cleared.credential_cleanup, "deleted")

    def test_rotating_a_credential_keeps_the_reference_and_replaces_only_the_value(self) -> None:
        first = self.controller.set_credential("alpha", "first-secret")
        reference = first.provider.credential_ref if first.provider else None
        second = self.controller.set_credential("alpha", "second-secret")
        self.assertEqual(second.provider.credential_ref if second.provider else None, reference)
        assert reference is not None
        self.assertEqual(self.credentials.resolve(reference), "second-secret")
        self.assertNotEqual(first.credential_fingerprint, second.credential_fingerprint)

        # A later model-write failure rolls the whole interactive setup back:
        # provider fields, model selection, and the overwritten keyring value.
        self.registry.put_model(ModelRecord("alpha", "stable"))
        self.controller.select_model("alpha", "stable")
        before = self.registry.provider("alpha")
        original_put_model = self.registry.put_model

        def fail_new_model(record: ModelRecord):
            if record.model_id == "broken":
                raise RegistryError("synthetic model failure")
            return original_put_model(record)

        with patch.object(self.registry, "put_model", side_effect=fail_new_model):
            with self.assertRaisesRegex(RegistryError, "synthetic model failure"):
                self.controller.configure_provider_model(
                    ProviderRecord(
                        provider_id="alpha",
                        adapter_kind=before.adapter_kind,
                        base_url="https://changed.example/v1",
                        credential_ref=before.credential_ref,
                        privacy_class=before.privacy_class,
                    ),
                    ModelRecord("alpha", "broken"),
                    secret="third-secret",
                    credential_name="replacement-alpha",
                )
        self.assertEqual(self.registry.provider("alpha"), before)
        self.assertEqual(self.registry.selected_model().model_id, "stable")
        self.assertEqual(self.credentials.resolve(reference), "second-secret")
        self.assertNotIn(
            ("KaroX/provider", "replacement-alpha"),
            self.backend.values,
        )

    def test_removing_the_selected_model_repairs_selection_deterministically(self) -> None:
        for model in ("zeta", "alpha", "middle"):
            self.registry.put_model(ModelRecord("alpha", model))
        self.controller.select_model("alpha", "middle")
        result = self.controller.remove_model("alpha", "middle")
        self.assertEqual(result.model.model_id if result.model else None, "middle")
        self.assertEqual(
            result.selected_model.model_id if result.selected_model else None,
            "alpha",
        )
        self.assertEqual(self.registry.selected_model().model_id, "alpha")

    def test_provider_removal_preserves_shared_secret_then_deletes_last_owner(self) -> None:
        first = self.controller.set_credential(
            "alpha", "shared-secret", credential_name="shared"
        )
        reference = first.provider.credential_ref if first.provider else None
        assert reference is not None
        self.registry.put_provider(
            ProviderRecord(
                provider_id="beta",
                adapter_kind="openai_responses",
                base_url="https://beta.example/v1",
                credential_ref=reference,
            )
        )
        shared = self.controller.remove_provider(
            "alpha", cascade=True, delete_credential=True
        )
        self.assertEqual(shared.credential_cleanup, "shared_not_deleted")
        self.assertEqual(self.credentials.resolve(reference), "shared-secret")

        last = self.controller.remove_provider(
            "beta", cascade=True, delete_credential=True
        )
        self.assertEqual(last.credential_cleanup, "deleted")
        self.assertEqual(self.backend.deleted, [("KaroX/provider", "shared")])


if __name__ == "__main__":
    unittest.main()
