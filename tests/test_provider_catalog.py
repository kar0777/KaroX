"""The shipped models.dev catalog and the key-only connect flow.

No test touches the network or the OS keyring: discovery runs over an
``httpx.MockTransport`` and credentials are ``env:`` references.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import httpx
import pytest

from karox import cli
from karox.credentials import CredentialStore
from karox.provider_catalog import (
    catalog_model_records,
    load_catalog_document,
    plausible_output_limit,
    provider_catalog,
)
from karox.provider_connect import ConnectError, connect_from_catalog, sync_from_catalog
from karox.provider_controller import ProviderController
from karox.provider_presets import PROVIDER_PRESETS
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry

KEY_ENV = "KAROX_PROVIDER_ACME_API_KEY"


def _row(name, context, output, price_in=None, price_out=None, *, released="2026-01-01", status=""):
    return [name, context, output, False, True, price_in, price_out, None, None, released, status]


def _document() -> dict:
    return {
        "schema": 1,
        "fetched_at": "2026-11-03",
        "source": "https://models.dev/api.json",
        "license": "MIT",
        "providers": {
            "acme": {
                "name": "Acme AI",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://api.acme.test/v1",
                "env": ["ACME_API_KEY"],
                "doc": "https://acme.test/docs",
                "models": {
                    "acme-large": _row("Acme Large", 200000, 32000, 1.0, 4.0, released="2026-06-01"),
                    "acme-small": _row("Acme Small", 128000, 128000, 0.1, 0.4),
                    "acme-old": _row("Acme Old", 32000, 4000, 0.5, 1.0, status="deprecated"),
                },
            },
            "tenant": {
                "name": "Tenant Cloud",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://${TENANT}.tenant.test/v1",
                "env": ["TENANT_KEY"],
                "models": {"t-1": _row("T1", 64000, 8000)},
            },
            "bedrock": {
                "name": "Bedrock",
                "npm": "@ai-sdk/amazon-bedrock",
                "env": ["AWS_ACCESS_KEY_ID"],
                "models": {"m": _row("M", 1000, 100)},
            },
            "exotic": {
                "name": "Exotic",
                "npm": "exotic-sdk",
                "api": "https://exotic.test",
                "models": {},
            },
        },
    }


@pytest.fixture()
def catalog():
    return load_catalog_document(_document())


@pytest.fixture()
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderController:
    monkeypatch.setenv(KEY_ENV, "sk-test-acme")
    registry = ProviderRegistry(tmp_path / "providers.json")
    return ProviderController(registry=registry, credentials=CredentialStore())


def _models_transport(ids, *, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(request.url), request.headers.get("authorization", "")))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "invalid api key"}})
        return httpx.Response(200, json={"object": "list", "data": [{"id": item} for item in ids]})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- snapshot


def test_shipped_snapshot_is_dated_and_broad() -> None:
    shipped = provider_catalog()
    assert shipped.fetched_at.count("-") == 2
    assert shipped.pricing_version == "models-dev-" + shipped.fetched_at.replace("-", "")
    installable = [item for item in shipped.providers if item.installable]
    assert len(shipped.providers) >= 100
    assert len(installable) >= 100
    for provider_id in ("deepseek", "groq", "mistral", "xai", "togetherai", "wandb"):
        assert shipped.provider(provider_id).installable, provider_id


def test_verified_presets_win_over_catalog_endpoints() -> None:
    shipped = provider_catalog()
    for provider_id in ("openrouter", "openai", "anthropic"):
        entry = shipped.provider(provider_id)
        assert entry.preset is True
        assert entry.base_url == PROVIDER_PRESETS[provider_id].base_url
        assert entry.adapter_kind == PROVIDER_PRESETS[provider_id].adapter_kind
    google = shipped.provider("google")
    assert google.adapter_kind == "gemini_generate_content"


def test_unsupported_providers_say_why(catalog) -> None:
    bedrock = catalog.provider("bedrock")
    assert not bedrock.installable
    assert "signing" in bedrock.unsupported_reason
    exotic = catalog.provider("exotic")
    assert not exotic.installable
    assert "exotic-sdk" in exotic.unsupported_reason
    assert [item.provider_id for item in catalog.search(installable_only=True)] == ["acme", "tenant"]


def test_endpoint_placeholders_are_filled_and_validated(catalog) -> None:
    tenant = catalog.provider("tenant")
    assert tenant.url_variables == ("TENANT",)
    assert tenant.resolved_base_url({"TENANT": "blue-1"}) == "https://blue-1.tenant.test/v1"
    with pytest.raises(ValueError, match="TENANT"):
        tenant.resolved_base_url({})
    with pytest.raises(ValueError, match="characters"):
        tenant.resolved_base_url({"TENANT": "evil.test/#"})


def test_output_limits_equal_to_the_window_are_not_trusted() -> None:
    assert plausible_output_limit(200000, 32000) == 32000
    assert plausible_output_limit(128000, 128000) is None
    assert plausible_output_limit(None, 8000) == 8000
    assert plausible_output_limit(64000, None) is None


def test_catalog_records_carry_price_provenance(catalog) -> None:
    records = {item.model_id: item for item in catalog_model_records(catalog.provider("acme"), "acme", catalog)}
    assert set(records) == {"acme-large", "acme-small"}  # deprecated hidden
    large = records["acme-large"]
    assert large.pricing is not None
    assert large.pricing.version == "models-dev-20261103"
    assert "published list price" in large.pricing.source
    assert large.provenance == "catalog:models.dev@2026-11-03"
    assert large.tools == "true"
    assert records["acme-small"].max_output_tokens is None


# ----------------------------------------------------------------- connect


def test_connect_narrows_to_listed_models_and_keeps_catalog_prices(controller, catalog) -> None:
    seen: list = []
    result = connect_from_catalog(
        controller,
        "acme",
        credential_ref=f"env:{KEY_ENV}",
        catalog=catalog,
        transport=_models_transport(["acme-large", "acme-embed"], seen=seen),
    )
    assert seen and seen[0][0] == "https://api.acme.test/v1/models"
    assert seen[0][1] == "Bearer sk-test-acme"
    assert result.activated is True
    assert result.model_id == "acme-large"
    assert result.verification["status"] == "ok"
    models = {item.model_id: item for item in controller.registry.models("acme")}
    # acme-small is not offered to this account; acme-embed has no tool support.
    assert set(models) == {"acme-large"}
    assert models["acme-large"].pricing is not None
    assert models["acme-large"].pricing.input_per_million == 1.0
    assert models["acme-large"].provenance.endswith("catalog:models.dev@2026-11-03")
    provider = controller.registry.provider("acme")
    assert provider.credential_ref == f"env:{KEY_ENV}"
    assert provider.base_url == "https://api.acme.test/v1"
    selected = controller.registry.selected_model()
    assert (selected.provider_id, selected.model_id) == ("acme", "acme-large")


def test_connect_rejected_key_writes_nothing(controller, catalog) -> None:
    with pytest.raises(ConnectError, match="rejected the API key"):
        connect_from_catalog(
            controller,
            "acme",
            credential_ref=f"env:{KEY_ENV}",
            catalog=catalog,
            transport=_models_transport([], status=401),
        )
    assert controller.registry.providers() == []


def test_connect_does_not_steal_the_selection_from_another_provider(controller, catalog) -> None:
    controller.registry.put_provider(
        ProviderRecord(provider_id="other", adapter_kind="openai_compatible_chat", base_url="https://other.test/v1")
    )
    controller.registry.put_model(ModelRecord(provider_id="other", model_id="m1"))
    controller.registry.select_model("other", "m1")
    result = connect_from_catalog(
        controller, "acme", credential_ref=f"env:{KEY_ENV}", catalog=catalog, test=False
    )
    assert result.activated is False
    assert controller.registry.selected_model().provider_id == "other"
    explicit = connect_from_catalog(
        controller, "acme", credential_ref=f"env:{KEY_ENV}", catalog=catalog, test=False,
        model="acme-small",
    )
    assert explicit.activated is True
    assert controller.registry.selected_model().model_id == "acme-small"


def test_connect_refuses_unknown_models_and_unsupported_providers(controller, catalog) -> None:
    with pytest.raises(ConnectError, match="not offered"):
        connect_from_catalog(
            controller, "acme", credential_ref=f"env:{KEY_ENV}", catalog=catalog, test=False,
            model="acme-nope",
        )
    with pytest.raises(ConnectError, match="signing"):
        connect_from_catalog(controller, "bedrock", credential_ref=f"env:{KEY_ENV}", catalog=catalog)
    with pytest.raises(ConnectError, match="needs an API key"):
        connect_from_catalog(controller, "acme", catalog=catalog, test=False)


def test_catalog_sync_fills_only_missing_fields(controller, catalog) -> None:
    controller.registry.put_provider(
        ProviderRecord(provider_id="acme", adapter_kind="openai_compatible_chat", base_url="https://api.acme.test/v1")
    )
    controller.registry.put_model(ModelRecord(provider_id="acme", model_id="acme-large", context_window=100000))
    report = sync_from_catalog(controller.registry, catalog=catalog)
    assert report["models_updated"] == 1
    model = controller.registry.model("acme", "acme-large")
    assert model.context_window == 100000  # the user's value stays
    assert model.max_output_tokens == 32000
    assert model.pricing is not None and model.pricing.output_per_million == 4.0
    again = sync_from_catalog(controller.registry, catalog=catalog)
    assert again["models_updated"] == 0


# --------------------------------------------------------------------- CLI


def test_cli_catalog_and_connect_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KAROX_PROVIDER_DEEPSEEK_API_KEY", "sk-test")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "catalog", "deepseek", "--json"]) == 0
    rows = json.loads(output.getvalue())
    assert [row["provider_id"] for row in rows] == ["deepseek"]
    assert rows[0]["installable"] is True and rows[0]["connected_as"] is None
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "connect", "deepseek", "--env-key", "--no-test", "--json"]) == 0
    payload = json.loads(output.getvalue())
    assert payload["provider_id"] == "deepseek"
    assert payload["activated"] is True
    assert payload["priced_models"] >= 1
    assert "sk-test" not in output.getvalue()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "catalog", "deepseek", "--json"]) == 0
    assert json.loads(output.getvalue())[0]["connected_as"] == "deepseek"
