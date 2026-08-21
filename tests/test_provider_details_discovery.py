"""Provider details: honest capability rows and model discovery.

``model_capability_summary`` must render the registry's true/false/unknown
contract without guessing, and ``discover_models_for_provider`` must add only
new models, carry ``provenance="discovered"``, follow the effective base URL
the way ``karox model discover`` does, and never leak the credential it
resolved into its secret-free summary.
"""

import unittest
from unittest import mock

from karox.registry import ModelRecord
from karox.tui_connections import (
    discover_models_for_provider,
    model_capability_summary,
)


class _Provider:
    def __init__(self) -> None:
        self.provider_id = "acme"
        self.adapter_kind = "openai_compatible_chat"
        self.base_url = "https://api.acme.test"
        self.credential_ref = "os-keyring:provider/acme"


class _Details:
    def __init__(self, provider, models) -> None:
        self.provider = provider
        self.models = tuple(models)


class _Credentials:
    def __init__(self, secret="sk-test", error=None) -> None:
        self._secret = secret
        self._error = error

    def resolve(self, reference):
        if self._error is not None:
            raise self._error
        return self._secret


class _Controller:
    def __init__(self, provider, models, credentials) -> None:
        self._details = _Details(provider, models)
        self.credentials = credentials
        self.put_calls = []
        self.edit_calls = []

    def details(self, provider_id):
        return self._details

    def put_model(self, record):
        self.put_calls.append(record)

    def edit_provider(self, provider_id, **kwargs):
        self.edit_calls.append((provider_id, kwargs))


class ModelCapabilitySummaryTests(unittest.TestCase):
    def test_renders_true_false_and_unknown_without_guessing(self) -> None:
        model = ModelRecord(
            provider_id="acme",
            model_id="m1",
            tools="true",
            vision="false",
            structured_output="unknown",
            streaming="true",
        )
        self.assertEqual(
            model_capability_summary(model), "tools✓ vision✗ json? stream✓"
        )

    def test_missing_attributes_stay_unknown(self) -> None:
        class Bare:
            pass

        self.assertEqual(
            model_capability_summary(Bare()), "tools? vision? json? stream?"
        )


class DiscoverModelsForProviderTests(unittest.TestCase):
    def _discovery(self, model_ids, base_url="https://api.acme.test"):
        from karox.tui import DiscoveredModel, ModelDiscovery

        return ModelDiscovery(
            models=tuple(DiscoveredModel(model_id=item) for item in model_ids),
            base_url=base_url,
            attempted_urls=(base_url + "/models",),
        )

    def test_adds_only_new_models_with_discovered_provenance(self) -> None:
        provider = _Provider()
        existing = ModelRecord(provider_id="acme", model_id="old")
        controller = _Controller(provider, [existing], _Credentials())
        with mock.patch(
            "karox.tui._discover_models_result",
            return_value=self._discovery(["old", "new"]),
        ):
            result = discover_models_for_provider(controller, "acme")
        self.assertEqual(result["discovered"], 2)
        self.assertEqual(result["added"], ["new"])
        self.assertEqual(len(controller.put_calls), 1)
        record = controller.put_calls[0]
        self.assertEqual(record.model_id, "new")
        self.assertEqual(record.provenance, "discovered")
        self.assertEqual(record.provider_id, "acme")
        self.assertNotIn("sk-test", repr(result))
        self.assertEqual(controller.edit_calls, [])

    def test_follows_effective_base_url_like_the_cli(self) -> None:
        provider = _Provider()
        controller = _Controller(provider, [], _Credentials())
        with mock.patch(
            "karox.tui._discover_models_result",
            return_value=self._discovery(["m"], base_url="https://api.acme.test/v1"),
        ):
            result = discover_models_for_provider(controller, "acme")
        self.assertEqual(
            controller.edit_calls,
            [("acme", {"base_url": "https://api.acme.test/v1"})],
        )
        self.assertEqual(result["base_url"], "https://api.acme.test/v1")

    def test_credential_failure_degrades_to_anonymous_discovery(self) -> None:
        provider = _Provider()
        controller = _Controller(
            provider, [], _Credentials(error=RuntimeError("keyring locked"))
        )
        captured = {}

        def fake(setup):
            captured["api_key"] = setup.api_key
            return self._discovery([])

        with mock.patch("karox.tui._discover_models_result", side_effect=fake):
            discover_models_for_provider(controller, "acme")
        self.assertEqual(captured["api_key"], "")


if __name__ == "__main__":
    unittest.main()
