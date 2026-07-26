from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.registry import (
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
    RegistryError,
)


def provider(provider_id: str = "local") -> ProviderRecord:
    return ProviderRecord(
        provider_id=provider_id,
        adapter_kind="openai_compatible_chat",
        base_url="http://127.0.0.1:8000/v1",
        privacy_class="local",
    )


def model(
    model_id: str = "model-a", *, aliases: tuple[str, ...] = ("default",)
) -> ModelRecord:
    return ModelRecord(
        provider_id="local",
        model_id=model_id,
        aliases=aliases,
        tools="true",
        streaming="true",
        pricing=ModelPricing("2026-07", "USD", 1.5, 4.0, "test fixture"),
    )


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_deterministic_and_resolves_aliases(self) -> None:
        self.registry.put_provider(provider("z-provider"))
        self.registry.put_provider(provider())
        self.registry.put_model(model("z-model", aliases=("z",)))
        self.registry.put_model(model())

        self.assertEqual(
            [item.provider_id for item in self.registry.providers()],
            ["local", "z-provider"],
        )
        self.assertEqual(
            [item.model_id for item in self.registry.models("local")],
            ["model-a", "z-model"],
        )
        selected = self.registry.model("local", "default")
        self.assertEqual(selected.model_id, "model-a")
        assert selected.pricing is not None
        self.assertEqual(
            selected.pricing.estimate(
                {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
            ),
            3.5,
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["providers"][0]["provider_id"], "local")
        self.assertEqual(payload["models"][0]["model_id"], "model-a")

    def test_requires_provider_and_model_arrays(self) -> None:
        invalid_values = (
            {"schema_version": 1, "providers": {}, "models": []},
            {"schema_version": 1, "providers": [], "models": {}},
            {"schema_version": 1, "providers": []},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(RegistryError, "must be arrays"):
                    self.registry.providers()

    def test_rejects_collisions_across_model_ids_and_aliases(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model("first", aliases=("shared",)))

        for record in (
            model("shared", aliases=()),
            model("second", aliases=("shared",)),
            model("third", aliases=("third",)),
        ):
            with (
                self.subTest(record=record),
                self.assertRaisesRegex(RegistryError, "colliding model IDs or aliases"),
            ):
                self.registry.put_model(record)

    def test_save_validates_orphans_before_writing(self) -> None:
        orphan = ModelRecord(provider_id="missing", model_id="orphan")
        with self.assertRaisesRegex(RegistryError, "orphaned"):
            self.registry._save([provider()], [orphan])
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_preserves_previous_registry(self) -> None:
        self.registry.put_provider(provider())
        original = self.path.read_bytes()

        with patch("karox.registry.os.replace", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                self.registry.put_provider(provider("another"))

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_a_cached_prompt_is_not_billed_as_a_fresh_one(self) -> None:
        pricing = ModelPricing("2026-07", "USD", 5.0, 25.0, "test fixture")

        fresh = pricing.estimate(
            {"prompt_tokens": 100_000, "completion_tokens": 1_000}
        )
        cached = pricing.estimate(
            {
                "prompt_tokens": 100_000,
                "completion_tokens": 1_000,
                "cache_read_tokens": 95_000,
            }
        )

        # prompt_tokens is the whole prompt; charging all of it at the input
        # rate reported a cost the run did not incur once caching existed.
        self.assertEqual(fresh, 0.525)
        self.assertEqual(cached, 0.0975)

    def test_a_cache_write_costs_more_than_plain_input(self) -> None:
        pricing = ModelPricing("2026-07", "USD", 5.0, 25.0, "test fixture")

        written = pricing.estimate(
            {"prompt_tokens": 1_000_000, "cache_write_tokens": 1_000_000}
        )

        self.assertEqual(written, 6.25)

    def test_explicit_cache_rates_beat_the_assumed_multipliers(self) -> None:
        pricing = ModelPricing(
            "2026-07",
            "USD",
            5.0,
            25.0,
            "test fixture",
            cache_read_per_million=0.25,
            cache_write_per_million=7.5,
        )

        self.assertEqual(pricing.cache_read_rate, 0.25)
        self.assertEqual(pricing.cache_write_rate, 7.5)
        self.assertEqual(
            pricing.estimate(
                {"prompt_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
            ),
            0.25,
        )

    def test_pricing_rejects_invalid_currency_and_non_finite_values(self) -> None:
        for arguments in (
            ("v1", "usd", 1.0, 2.0, "source"),
            ("v1", "USD", float("nan"), 2.0, "source"),
            ("v1", "USD", 1.0, -1.0, "source"),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ModelPricing(*arguments)

    def test_provider_rejects_non_text_header_and_query_entries(self) -> None:
        cases = (
            {"headers": {1: "value"}},
            {"headers": {"X-Test": 1}},
            {"query": {1: "value"}},
            {"query": {"api-version": 1}},
        )
        for options in cases:
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "must contain only text"),
            ):
                ProviderRecord(
                    provider_id="typed",
                    adapter_kind="openai_compatible_chat",
                    base_url="https://provider.example/v1",
                    **options,  # type: ignore[arg-type]
                )

    def test_selection_is_canonical_and_persistent(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())

        selected = self.registry.select_model("local", "default")

        self.assertEqual(selected.model_id, "model-a")
        reloaded = ProviderRegistry(self.path).selected_model()
        assert reloaded is not None
        self.assertEqual(
            (reloaded.provider_id, reloaded.model_id), ("local", "model-a")
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["selected_model"],
            {"provider_id": "local", "model_id": "model-a"},
        )

    def test_map_alias_creates_and_moves_provider_scoped_alias(self) -> None:
        self.registry.put_provider(provider())
        first = self.registry.map_alias("local", "sol", "actual-sol-a")
        self.assertEqual(first.aliases, ("sol",))
        second = self.registry.map_alias("local", "sol", "actual-sol-b")
        self.assertIn("sol", second.aliases)
        self.assertNotIn("sol", self.registry.model("local", "actual-sol-a").aliases)
        self.assertEqual(self.registry.model("local", "sol").model_id, "actual-sol-b")

    def test_removal_requires_cascade_and_clears_selection(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())
        self.registry.select_model("local", "default")

        with self.assertRaisesRegex(RegistryError, "use cascade"):
            self.registry.remove_provider("local")
        self.assertIsNotNone(self.registry.selected_model())

        self.registry.remove_provider("local", cascade=True)
        self.assertEqual(self.registry.providers(), [])
        self.assertEqual(self.registry.models(), [])
        self.assertIsNone(self.registry.selected_model())

    def test_removing_selected_model_clears_selection(self) -> None:
        self.registry.put_provider(provider())
        self.registry.put_model(model())
        self.registry.select_model("local", "default")

        removed = self.registry.remove_model("local", "default")

        self.assertEqual(removed.model_id, "model-a")
        self.assertIsNone(self.registry.selected_model())

    def test_rejects_dangling_selected_model_on_load(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "providers": [asdict(provider())],
                    "models": [],
                    "selected_model": {
                        "provider_id": "local",
                        "model_id": "missing",
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RegistryError, "selected model does not exist"):
            self.registry.selected_model()


if __name__ == "__main__":
    unittest.main()
