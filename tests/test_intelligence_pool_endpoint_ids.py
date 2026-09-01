from __future__ import annotations

from pathlib import Path

from karox.intelligence_pool import IntelligencePool
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry


def _registry(tmp_path: Path, model_ids: list[str]) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "providers.json")
    registry.put_provider(
        ProviderRecord(
            provider_id="openrouter",
            adapter_kind="openai_responses",
            base_url="https://api.example.test/v1",
        )
    )
    for model_id in model_ids:
        registry.put_model(ModelRecord(provider_id="openrouter", model_id=model_id))
    return registry


def test_api_endpoint_keeps_safe_legacy_id_unchanged(tmp_path: Path) -> None:
    pool = IntelligencePool(
        path=tmp_path / "pool.json",
        provider_registry=_registry(tmp_path, ["strong"]),
    )
    endpoint = pool.api_endpoints()[0]
    assert endpoint.endpoint_id == "api:openrouter:strong"
    assert endpoint.model_id == "strong"


def test_provider_opaque_model_id_is_not_used_as_raw_karox_object_id(tmp_path: Path) -> None:
    raw = "~anthropic/claude-fable-latest"
    pool = IntelligencePool(
        path=tmp_path / "pool.json",
        provider_registry=_registry(tmp_path, [raw]),
    )
    endpoint = pool.api_endpoints()[0]
    assert endpoint.model_id == raw
    assert endpoint.endpoint_id.startswith("api:openrouter:_anthropic/claude-fable-latest-")
    assert "~" not in endpoint.endpoint_id
    assert len(endpoint.endpoint_id) <= 256
    assert pool.get(endpoint.endpoint_id).model_id == raw


def test_long_model_ids_get_stable_bounded_endpoint_keys(tmp_path: Path) -> None:
    raw = "vendor/" + ("x" * 400)
    registry = _registry(tmp_path, [raw])
    first = IntelligencePool(
        path=tmp_path / "pool.json", provider_registry=registry
    ).api_endpoints()[0]
    second = IntelligencePool(
        path=tmp_path / "pool.json", provider_registry=registry
    ).api_endpoints()[0]
    assert first.endpoint_id == second.endpoint_id
    assert len(first.endpoint_id) == 256
    assert first.model_id == raw


def test_sanitized_model_ids_use_hash_to_avoid_collisions(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["~vendor/model", "?vendor/model"])
    endpoints = IntelligencePool(
        path=tmp_path / "pool.json", provider_registry=registry
    ).api_endpoints()
    assert len(endpoints) == 2
    assert len({item.endpoint_id for item in endpoints}) == 2
    assert {item.model_id for item in endpoints} == {"~vendor/model", "?vendor/model"}
