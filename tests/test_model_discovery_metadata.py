"""Model discovery carries honest catalog metadata end to end.

The discovery wire (``karox.tui._discover_models_result``) must normalize
whatever a real catalog publishes -- OpenRouter-style capability lists and
per-token prices, or nothing but ids -- without ever inventing a capability,
and ``discovered_model_record`` must persist those verdicts and prices into
the registry with visible provenance. The failure taxonomy stays structured:
unauthorized, malformed, empty, timeout, and unsupported endpoints each keep
their own kind, and none of them turns into a fake catalog.
"""

import unittest
from unittest import mock

import httpx

from karox.registry import ModelRecord
from karox.tui import (
    DiscoveredModel,
    ModelDiscoveryError,
    ProviderSetup,
    _discover_models_result,
)
from karox.tui_connections import (
    discover_models_for_provider,
    discovered_model_record,
    model_capability_summary,
)


def _setup(base_url="https://api.example.test/v1"):
    return ProviderSetup(
        provider_id="openrouter",
        adapter="openai_compatible_chat",
        base_url=base_url,
        model_id="",
        api_key="k",
    )


def _response(status_code=200, payload=None, text=""):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    if payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    return response


OPENROUTER_CATALOG = {
    "data": [
        {
            "id": "vendor/free-model:free",
            "name": "Vendor: Free Model",
            "context_length": 131072,
            "supported_parameters": ["tools", "response_format"],
            "architecture": {"input_modalities": ["text", "image"]},
            "pricing": {
                "prompt": "0",
                "completion": "0",
                "input_cache_read": "0",
            },
        },
        {
            "id": "vendor/paid-model",
            "supported_parameters": ["temperature"],
            "architecture": {"input_modalities": ["text"]},
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        },
    ]
}


class DiscoveryMetadataTests(unittest.TestCase):
    def _discover(self, payload):
        with mock.patch(
            "karox.tui.httpx.get", return_value=_response(payload=payload)
        ):
            return _discover_models_result(_setup())

    def test_metadata_rich_catalog_is_normalized_honestly(self) -> None:
        result = self._discover(OPENROUTER_CATALOG)
        by_id = {item.model_id: item for item in result.models}
        free = by_id["vendor/free-model:free"]
        self.assertEqual(free.display_name, "Vendor: Free Model")
        self.assertEqual(free.context_window, 131072)
        self.assertEqual(free.tools, "true")
        self.assertEqual(free.structured_output, "true")
        self.assertEqual(free.vision, "true")
        # No catalog evidence about streaming: stays unknown, never guessed.
        self.assertEqual(free.streaming, "unknown")
        self.assertEqual(free.input_per_million, 0.0)
        self.assertEqual(free.output_per_million, 0.0)
        self.assertIs(free.free, True)
        paid = by_id["vendor/paid-model"]
        self.assertIsNone(paid.display_name)
        # The published list omits these: that is evidence of absence.
        self.assertEqual(paid.tools, "false")
        self.assertEqual(paid.structured_output, "false")
        self.assertEqual(paid.vision, "false")
        self.assertEqual(paid.input_per_million, 1.0)
        self.assertEqual(paid.output_per_million, 2.0)
        self.assertIs(paid.free, False)

    def test_id_only_catalog_stays_unknown_everywhere(self) -> None:
        result = self._discover({"data": [{"id": "m1"}, {"id": "m2"}]})
        for item in result.models:
            self.assertEqual(item.tools, "unknown")
            self.assertEqual(item.vision, "unknown")
            self.assertEqual(item.structured_output, "unknown")
            self.assertEqual(item.streaming, "unknown")
            self.assertIsNone(item.input_per_million)
            self.assertIsNone(item.output_per_million)
            self.assertIsNone(item.free)
            self.assertIsNone(item.display_name)

    def test_unauthorized_is_a_structured_http_failure(self) -> None:
        with mock.patch(
            "karox.tui.httpx.get",
            return_value=_response(status_code=401, payload={"error": "no"}),
        ):
            with self.assertRaises(ModelDiscoveryError) as caught:
                _discover_models_result(_setup())
        self.assertEqual(caught.exception.kind, "http")
        self.assertEqual(caught.exception.status_code, 401)

    def test_malformed_response_is_invalid_not_empty(self) -> None:
        with mock.patch(
            "karox.tui.httpx.get", return_value=_response(payload=None)
        ):
            with self.assertRaises(ModelDiscoveryError) as caught:
                _discover_models_result(_setup())
        self.assertEqual(caught.exception.kind, "invalid_response")

    def test_empty_catalog_keeps_its_own_kind(self) -> None:
        with mock.patch(
            "karox.tui.httpx.get", return_value=_response(payload={"data": []})
        ):
            with self.assertRaises(ModelDiscoveryError) as caught:
                _discover_models_result(_setup())
        self.assertEqual(caught.exception.kind, "empty")

    def test_timeout_keeps_its_own_kind(self) -> None:
        with mock.patch(
            "karox.tui.httpx.get",
            side_effect=httpx.TimeoutException("too slow"),
        ):
            with self.assertRaises(ModelDiscoveryError) as caught:
                _discover_models_result(_setup())
        self.assertEqual(caught.exception.kind, "timeout")

    def test_unsupported_models_endpoint_stays_an_http_failure(self) -> None:
        with mock.patch(
            "karox.tui.httpx.get",
            return_value=_response(status_code=404, payload={}),
        ):
            with self.assertRaises(ModelDiscoveryError) as caught:
                _discover_models_result(_setup())
        self.assertEqual(caught.exception.kind, "http")
        self.assertEqual(caught.exception.status_code, 404)


class DiscoveredModelRecordTests(unittest.TestCase):
    def test_metadata_and_pricing_reach_the_registry_record(self) -> None:
        item = DiscoveredModel(
            model_id="vendor/free-model:free",
            context_window=131072,
            tools="true",
            vision="true",
            structured_output="true",
            input_per_million=0.0,
            output_per_million=0.0,
            cache_read_per_million=0.0,
            free=True,
        )
        record = discovered_model_record("openrouter", item)
        self.assertEqual(record.provenance, "discovered")
        self.assertEqual(record.tools, "true")
        self.assertEqual(record.vision, "true")
        self.assertEqual(record.structured_output, "true")
        self.assertEqual(record.streaming, "unknown")
        assert record.pricing is not None
        self.assertEqual(record.pricing.source, "provider-reported")
        self.assertEqual(record.pricing.input_per_million, 0.0)
        self.assertIn("free", model_capability_summary(record))

    def test_id_only_item_persists_without_invented_metadata(self) -> None:
        record = discovered_model_record("acme", DiscoveredModel(model_id="m1"))
        self.assertIsNone(record.pricing)
        self.assertEqual(record.tools, "unknown")
        self.assertEqual(record.streaming, "unknown")
        self.assertEqual(
            model_capability_summary(record), "tools? vision? json? stream?"
        )

    def test_paid_prices_render_in_the_summary(self) -> None:
        record = discovered_model_record(
            "acme",
            DiscoveredModel(
                model_id="m-paid", input_per_million=1.0, output_per_million=2.0
            ),
        )
        self.assertIn("$1/M in $2/M out", model_capability_summary(record))


class DiscoverForProviderMetadataTests(unittest.TestCase):
    def test_new_models_persist_catalog_metadata(self) -> None:
        class Provider:
            provider_id = "acme"
            adapter_kind = "openai_compatible_chat"
            base_url = "https://api.acme.test"
            credential_ref = None

        class Details:
            provider = Provider()
            models = (ModelRecord(provider_id="acme", model_id="old"),)

        class Controller:
            def __init__(self):
                self.put_calls = []
                self.credentials = mock.Mock()

            def details(self, provider_id):
                return Details()

            def put_model(self, record):
                self.put_calls.append(record)

            def edit_provider(self, provider_id, **kwargs):
                raise AssertionError("base URL did not change")

        from karox.tui import ModelDiscovery

        discovery = ModelDiscovery(
            models=(
                DiscoveredModel(
                    model_id="new",
                    tools="true",
                    input_per_million=0.0,
                    output_per_million=0.0,
                    free=True,
                ),
                DiscoveredModel(model_id="old", tools="true"),
            ),
            base_url="https://api.acme.test",
            attempted_urls=("https://api.acme.test/models",),
        )
        controller = Controller()
        with mock.patch(
            "karox.tui._discover_models_result", return_value=discovery
        ):
            result = discover_models_for_provider(controller, "acme")
        self.assertEqual(result["added"], ["new"])
        self.assertEqual(len(controller.put_calls), 1)
        record = controller.put_calls[0]
        self.assertEqual(record.tools, "true")
        assert record.pricing is not None
        self.assertEqual(record.pricing.input_per_million, 0.0)
        # The existing record is never overwritten with catalog metadata.
        self.assertEqual(record.model_id, "new")


if __name__ == "__main__":
    unittest.main()
