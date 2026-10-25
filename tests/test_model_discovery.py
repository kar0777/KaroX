"""Contracts for provider model discovery.

Every discovery test runs against an ``httpx.MockTransport``: no socket is
opened.  The tests pin the honest-metadata rules (unknown stays unknown,
missing price never becomes zero), the OpenAI ``/v1`` fallback, adapter
specific auth headers, duplicate handling, and the structured failure taxonomy.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from karox.model_discovery import (
    DiscoveredModel,
    ModelDiscoveryError,
    discover_models,
    discover_models_result,
    metadata_for_registry,
    model_record,
)
from karox.registry import ModelRecord, ProviderRecord


def provider(**overrides: object) -> ProviderRecord:
    values: dict[str, object] = {
        "provider_id": "acme",
        "adapter_kind": "openai_compatible_chat",
        "base_url": "https://api.acme.test/v1",
    }
    values.update(overrides)
    return ProviderRecord(**values)  # type: ignore[arg-type]


def transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


OPENROUTER_STYLE_CATALOG = {
    "data": [
        {
            "id": "vendor/paid-model",
            "context_length": 131072,
            "max_output_tokens": 8192,
            "supported_parameters": [
                "temperature",
                "response_format",
                "tools",
                "tool_choice",
            ],
            "architecture": {"input_modalities": ["text", "image"]},
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000001",
            },
        },
        {
            "id": "vendor/free-model:free",
            "name": "Vendor: Free Model",
            "top_provider": {
                "context_length": 64000,
                "max_completion_tokens": 4096,
            },
            "supported_parameters": ["temperature"],
            "architecture": {"input_modalities": ["text"]},
            "pricing": {"prompt": "0", "completion": "0"},
        },
    ]
}


def test_catalog_metadata_is_normalized_honestly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["accept"] == "application/json"
        assert "authorization" not in request.headers
        return json_response(OPENROUTER_STYLE_CATALOG)

    models = discover_models(provider(), None, transport=transport(handler))
    by_id = {item.model_id: item for item in models}

    assert [item.model_id for item in models] == [
        "vendor/free-model:free",
        "vendor/paid-model",
    ]
    free = by_id["vendor/free-model:free"]
    assert free.display_name == "Vendor: Free Model"
    assert free.context_window == 64000
    assert free.max_output_tokens == 4096
    assert free.supports_tools is False
    assert free.supports_structured_output is False
    assert free.supports_vision is False
    assert free.supports_streaming is None
    assert free.input_price_per_million == 0.0
    assert free.output_price_per_million == 0.0
    assert free.cache_read_price_per_million is None
    assert free.free is True
    assert free.provenance == "discovered"
    assert free.raw["id"] == "vendor/free-model:free"

    paid = by_id["vendor/paid-model"]
    assert paid.display_name is None
    assert paid.context_window == 131072
    assert paid.max_output_tokens == 8192
    assert paid.supports_tools is True
    assert paid.supports_structured_output is True
    assert paid.supports_vision is True
    assert paid.supports_streaming is None
    assert paid.input_price_per_million == 1.0
    assert paid.output_price_per_million == 2.0
    assert paid.cache_read_price_per_million == 0.1
    assert paid.free is False


def test_bearer_token_and_provider_headers_and_query_are_sent() -> None:
    reference = "os-keyring:provider/acme"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-live-1234567890abcdef"
        assert request.headers["x-tenant"] == "tenant-1"
        assert request.url.params["api-version"] == "2024-01-01"
        return json_response({"data": [{"id": "model-a"}]})

    discovered = discover_models(
        provider(
            credential_ref=reference,
            headers={"X-Tenant": "tenant-1"},
            query={"api-version": "2024-01-01"},
        ),
        lambda ref: "sk-live-1234567890abcdef" if ref == reference else "",
        transport=transport(handler),
    )
    assert [item.model_id for item in discovered] == ["model-a"]


def test_credential_store_like_resolver_and_anthropic_headers() -> None:
    class Resolver:
        def __init__(self) -> None:
            self.references: list[str] = []

        def resolve(self, reference: str) -> str:
            self.references.append(reference)
            return "anthropic-secret"

    resolver = Resolver()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "anthropic-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        return json_response({"data": [{"id": "claude-test"}]})

    discovered = discover_models(
        provider(
            adapter_kind="anthropic_messages",
            base_url="https://api.anthropic.test/v1",
            credential_ref="os-keyring:provider/anthropic",
        ),
        resolver,
        transport=transport(handler),
    )
    assert [item.model_id for item in discovered] == ["claude-test"]
    assert resolver.references == ["os-keyring:provider/anthropic"]


def test_gemini_catalog_and_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "gemini-secret"
        return json_response(
            {
                "models": [
                    {
                        "name": "models/gemini-test",
                        "displayName": "Gemini Test",
                        "inputTokenLimit": 100000,
                        "outputTokenLimit": 8192,
                    }
                ]
            }
        )

    discovered = discover_models(
        provider(
            adapter_kind="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            credential_ref="os-keyring:provider/gemini",
        ),
        lambda _reference: "gemini-secret",
        transport=transport(handler),
    )
    assert len(discovered) == 1
    item = discovered[0]
    assert item.model_id == "gemini-test"
    assert item.display_name == "Gemini Test"
    assert item.context_window == 100000
    assert item.max_output_tokens == 8192


def test_openai_base_url_retries_the_common_v1_path_on_404() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/models":
            return httpx.Response(404, text="not found")
        return json_response({"data": [{"id": "model-a"}]})

    result = discover_models_result(
        provider(base_url="https://api.acme.test"),
        None,
        transport=transport(handler),
    )
    assert result.base_url == "https://api.acme.test/v1"
    assert result.attempted_urls == (
        "https://api.acme.test/models",
        "https://api.acme.test/v1/models",
    )
    assert requested == ["/models", "/v1/models"]
    assert [item.model_id for item in result.models] == ["model-a"]


def test_non_404_failure_does_not_try_the_fallback() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(500, text="broken")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(
            provider(base_url="https://api.acme.test"),
            None,
            transport=transport(handler),
        )
    assert caught.value.kind == "http"
    assert caught.value.status_code == 500
    assert requested == ["/models"]
    assert caught.value.attempted_urls == ("https://api.acme.test/models",)


def test_http_error_is_structured_and_carries_the_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"error": {"message": "invalid API key"}}, 401)

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(handler))
    error = caught.value
    assert error.kind == "http"
    assert error.status_code == 401
    assert error.detail == "invalid API key"
    assert error.attempted_urls == ("https://api.acme.test/v1/models",)


def test_404_after_the_v1_fallback_keeps_both_attempts() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(
            provider(base_url="https://api.acme.test"),
            None,
            transport=transport(handler),
        )
    error = caught.value
    assert error.kind == "http"
    assert error.status_code == 404
    assert error.attempted_urls == (
        "https://api.acme.test/models",
        "https://api.acme.test/v1/models",
    )


def test_error_bodies_never_leak_the_credential() -> None:
    secret = "hunter2-not-a-real-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response(
            {"error": {"message": f"rejected credential {secret}"}}, 403
        )

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(
            provider(credential_ref="os-keyring:provider/acme"),
            lambda _reference: secret,
            transport=transport(handler),
        )
    error = caught.value
    assert secret not in str(error)
    assert secret not in error.detail
    assert "[REDACTED]" in error.detail


def test_malformed_json_missing_list_and_empty_catalog_keep_their_kinds() -> None:
    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not a catalog</html>")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(malformed))
    assert caught.value.kind == "invalid_response"

    def missing_list(_request: httpx.Request) -> httpx.Response:
        return json_response({"object": "list"})

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(missing_list))
    assert caught.value.kind == "invalid_response"

    def empty(_request: httpx.Request) -> httpx.Response:
        return json_response({"data": []})

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(empty))
    assert caught.value.kind == "empty"


def test_bare_list_catalog_is_accepted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response([{"id": "model-a"}])

    discovered = discover_models(provider(), None, transport=transport(handler))
    assert [item.model_id for item in discovered] == ["model-a"]


def test_timeout_and_network_failures_keep_their_kinds() -> None:
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(slow))
    assert caught.value.kind == "timeout"

    def offline(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(provider(), None, transport=transport(offline))
    assert caught.value.kind == "network"
    assert "no route" not in str(caught.value)


def test_duplicate_ids_are_collapsed_with_the_first_entry_winning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "data": [
                    {"id": "zeta", "context_length": 100},
                    {"id": "Alpha"},
                    {"id": "zeta", "context_length": 999},
                ]
            }
        )

    discovered = discover_models(provider(), None, transport=transport(handler))
    assert [item.model_id for item in discovered] == ["Alpha", "zeta"]
    assert discovered[1].context_window == 100


def test_unknown_and_partial_pricing_never_become_zero() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "data": [
                    {"id": "no-pricing"},
                    {
                        "id": "half-pricing",
                        "pricing": {"prompt": "0.000003"},
                    },
                    {
                        "id": "garbage-pricing",
                        "pricing": {"prompt": "abc", "completion": "-1"},
                    },
                    {
                        "id": "free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    },
                ]
            }
        )

    discovered = discover_models(provider(), None, transport=transport(handler))
    by_id = {item.model_id: item for item in discovered}

    assert by_id["no-pricing"].input_price_per_million is None
    assert by_id["no-pricing"].output_price_per_million is None
    assert by_id["no-pricing"].free is None
    assert by_id["half-pricing"].input_price_per_million == 3.0
    assert by_id["half-pricing"].output_price_per_million is None
    assert by_id["garbage-pricing"].input_price_per_million is None
    assert by_id["garbage-pricing"].output_price_per_million is None
    assert by_id["free"].input_price_per_million == 0.0
    assert by_id["free"].output_price_per_million == 0.0
    assert by_id["free"].free is True

    for model_id in ("no-pricing", "half-pricing", "garbage-pricing"):
        assert metadata_for_registry(by_id[model_id])["pricing"] is None


def test_id_only_entry_stays_unknown_everywhere() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"data": [{"id": "m1", "name": "m1"}]})

    item = discover_models(provider(), None, transport=transport(handler))[0]
    assert item.display_name is None
    assert item.context_window is None
    assert item.max_output_tokens is None
    assert item.supports_tools is None
    assert item.supports_vision is None
    assert item.supports_structured_output is None
    assert item.supports_streaming is None
    assert item.input_price_per_million is None
    assert item.output_price_per_million is None
    assert item.free is None

    metadata = metadata_for_registry(item)
    assert metadata["tools"] == "unknown"
    assert metadata["vision"] == "unknown"
    assert metadata["structured_output"] == "unknown"
    assert metadata["streaming"] == "unknown"
    assert metadata["pricing"] is None


def test_explicit_capabilities_win_and_published_lists_mean_absence() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "data": [
                    {
                        "id": "explicit",
                        "capabilities": {
                            "tools": True,
                            "vision": False,
                            "streaming": True,
                        },
                        "supported_parameters": [],
                    },
                    {
                        "id": "modality",
                        "architecture": {"modality": "text+image->text"},
                    },
                ]
            }
        )

    by_id = {
        item.model_id: item
        for item in discover_models(provider(), None, transport=transport(handler))
    }
    explicit = by_id["explicit"]
    assert explicit.supports_tools is True
    assert explicit.supports_vision is False
    assert explicit.supports_streaming is True
    # The published list exists and does not name structured output.
    assert explicit.supports_structured_output is False
    assert by_id["modality"].supports_vision is True


def test_metadata_for_registry_carries_verdicts_and_pricing_provenance() -> None:
    item = DiscoveredModel(
        model_id="vendor/model",
        display_name="Vendor Model",
        context_window=1000,
        max_output_tokens=100,
        input_price_per_million=1.0,
        output_price_per_million=2.0,
        cache_read_price_per_million=0.1,
        supports_tools=True,
        supports_vision=False,
    )
    metadata = metadata_for_registry(item)
    assert metadata["tools"] == "true"
    assert metadata["vision"] == "false"
    assert metadata["structured_output"] == "unknown"
    assert metadata["streaming"] == "unknown"
    assert metadata["provenance"] == "discovered"
    assert metadata["display_name"] == "Vendor Model"
    pricing = metadata["pricing"]
    assert pricing.source == "provider-reported"
    assert pricing.input_per_million == 1.0
    assert pricing.output_per_million == 2.0
    assert pricing.cache_read_per_million == 0.1

    record = model_record("acme", item)
    assert isinstance(record, ModelRecord)
    assert record.provider_id == "acme"
    assert record.model_id == "vendor/model"
    assert record.context_window == 1000
    assert record.max_output_tokens == 100
    assert record.tools == "true"
    assert record.vision == "false"
    assert record.provenance == "discovered"
    assert record.pricing == pricing


def test_missing_resolver_and_resolver_failures_are_structured() -> None:
    configured = provider(credential_ref="os-keyring:provider/acme")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(configured, None)
    assert caught.value.kind == "credential"
    assert caught.value.attempted_urls == ()

    def broken(_reference: str) -> str:
        raise RuntimeError("backend unavailable for secret-value-1234567890")

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(configured, broken)
    error = caught.value
    assert error.kind == "credential"
    assert "secret-value-1234567890" not in str(error)

    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(configured, lambda _reference: "")
    assert caught.value.kind == "credential"


def test_invalid_base_url_and_timeout_are_rejected() -> None:
    with pytest.raises(ModelDiscoveryError) as caught:
        discover_models(SimpleNamespace(base_url="ftp://example.test"), None)
    assert caught.value.kind == "invalid_base_url"

    with pytest.raises(ValueError):
        discover_models(provider(), None, timeout_seconds=0)


def test_as_dict_is_json_friendly_and_omits_raw() -> None:
    item = DiscoveredModel(
        model_id="m",
        display_name="Model",
        input_price_per_million=0.0,
        output_price_per_million=0.0,
        supports_tools=True,
        raw={"id": "m", "internal": "payload"},
    )
    payload = item.as_dict()
    assert payload["model_id"] == "m"
    assert payload["display_name"] == "Model"
    assert payload["free"] is True
    assert "raw" not in payload
    assert item.id == "m"