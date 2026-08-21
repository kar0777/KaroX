"""Model browser presentation contract (mandate: make /model excellent).

The browser must show honest metadata (unknown stays unknown), filter only on
explicit "true" verdicts, and stay usable from 40x12 to 160x45 terminals.
These tests exercise the pure helpers directly, so a regression in the
contract fails without booting the TUI.
"""

from __future__ import annotations

import unittest
from dataclasses import asdict

from _support import SRC  # noqa: F401

from karox.registry import ModelPricing, ModelRecord
from karox.tui_dashboard import (
    MODEL_BROWSER_FILTERS,
    apply_model_filters,
    model_browser_budget,
    model_browser_columns,
    model_detail_lines,
    model_free_state,
    model_row_text,
)


def _record(**kwargs: object) -> ModelRecord:
    base: dict = dict(provider_id="prov", model_id="model-a")
    base.update(kwargs)
    return ModelRecord(**base)


FREE = ModelPricing(
    version="discovered",
    currency="USD",
    input_per_million=0.0,
    output_per_million=0.0,
    source="provider-reported",
)
PAID = ModelPricing(
    version="discovered",
    currency="USD",
    input_per_million=2.5,
    output_per_million=10.0,
    cache_read_per_million=0.25,
    source="provider-reported",
)

MANDATED_WIDTHS = (40, 46, 60, 73, 80, 100, 120, 160)


class FreeStateTests(unittest.TestCase):
    def test_no_pricing_is_unknown_not_free(self) -> None:
        self.assertEqual(model_free_state(_record()), "unknown")

    def test_zero_priced_model_is_free(self) -> None:
        self.assertEqual(model_free_state(_record(pricing=FREE)), "true")

    def test_priced_model_is_paid(self) -> None:
        self.assertEqual(model_free_state(_record(pricing=PAID)), "false")


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = [
            _record(
                model_id="alpha",
                tools="true",
                pricing=FREE,
                display_name="Alpha One",
            ),
            _record(model_id="beta", tools="false", vision="true", pricing=PAID),
            _record(model_id="gamma"),  # every capability honestly unknown
        ]

    def _ids(self, query: str = "", active: set[str] | None = None) -> list[str]:
        rows = apply_model_filters(self.models, query, active or set())
        return [item.model_id for item in rows]

    def test_query_matches_id_display_name_and_provider(self) -> None:
        self.assertEqual(self._ids("alph"), ["alpha"])
        self.assertEqual(self._ids("one"), ["alpha"])  # display name
        self.assertEqual(len(self._ids("prov")), 3)  # provider id

    def test_capability_filter_requires_explicit_true(self) -> None:
        self.assertEqual(self._ids(active={"tools"}), ["alpha"])
        self.assertEqual(self._ids(active={"vision"}), ["beta"])

    def test_unknown_never_satisfies_any_filter(self) -> None:
        for name in MODEL_BROWSER_FILTERS:
            self.assertNotIn("gamma", self._ids(active={name}))

    def test_free_filter_uses_published_prices_only(self) -> None:
        self.assertEqual(self._ids(active={"free"}), ["alpha"])

    def test_filters_compose_with_search(self) -> None:
        self.assertEqual(self._ids(active={"free", "tools"}), ["alpha"])
        self.assertEqual(self._ids("beta", {"free"}), [])

    def test_large_catalog_filtering_stays_exact(self) -> None:
        catalog = [
            _record(model_id=f"m-{i}", tools="true" if i % 2 else "false")
            for i in range(500)
        ]
        hits = apply_model_filters(catalog, "m-4", {"tools"})
        expected = [i for i in range(500) if i % 2 and "m-4" in f"m-{i}"]
        self.assertEqual(len(hits), len(expected))
        self.assertTrue(
            all(item.tools == "true" and "m-4" in item.model_id for item in hits)
        )


class ResponsiveTests(unittest.TestCase):
    def test_columns_grow_monotonically_with_width(self) -> None:
        previous: tuple[str, ...] = ()
        for width in MANDATED_WIDTHS:
            columns = model_browser_columns(width)
            self.assertIn("id", columns)
            self.assertIn("caps", columns)
            self.assertGreaterEqual(len(columns), len(previous))
            previous = columns

    def test_small_terminal_shows_identity_without_pricing(self) -> None:
        for width in (40, 46):
            self.assertNotIn("price", model_browser_columns(width))
            row = model_row_text(_record(pricing=PAID), width)
            self.assertNotIn("$", row)
            self.assertIn("model-a", row)

    def test_large_terminal_shows_pricing_cache_and_provenance(self) -> None:
        columns = model_browser_columns(160)
        for column in ("name", "price", "cache", "provenance"):
            self.assertIn(column, columns)
        row = model_row_text(
            _record(pricing=PAID, display_name="Nice", provenance="discovered"),
            160,
        )
        self.assertIn("$2.5/$10", row)
        self.assertIn("$0.25", row)
        self.assertIn("disc", row)
        self.assertIn("Nice", row)

    def test_rows_fit_the_budget_at_every_mandated_size(self) -> None:
        record = _record(
            model_id="a-very-long-model-identifier-preview-0325-that-clips",
            pricing=PAID,
            display_name="An extremely verbose published display name",
        )
        for width in MANDATED_WIDTHS:
            row = model_row_text(record, width, selected=True)
            self.assertLessEqual(len(row), model_browser_budget(width))
            self.assertTrue(row.startswith("\u2713 "))

    def test_selected_mark_contract_survives(self) -> None:
        self.assertTrue(model_row_text(_record(), 80, selected=True).startswith("\u2713 "))
        self.assertTrue(model_row_text(_record(), 80).startswith("  "))


class HonestUnknownTests(unittest.TestCase):
    def test_unknown_capabilities_render_as_question_marks(self) -> None:
        self.assertIn("????", model_row_text(_record(), 80))

    def test_known_capabilities_render_letters_and_dashes(self) -> None:
        row = model_row_text(
            _record(tools="true", vision="false", streaming="true"), 80
        )
        self.assertIn("T-?S", row)

    def test_details_carry_provenance_and_pricing_source(self) -> None:
        text = "\n".join(
            model_detail_lines(_record(pricing=PAID, provenance="discovered"), "en")
        )
        self.assertIn("discovered", text)
        self.assertIn("provider-reported", text)
        self.assertIn("Cached $0.25/M", text)

    def test_details_admit_missing_pricing_and_name(self) -> None:
        text = "\n".join(model_detail_lines(_record(), "en"))
        self.assertIn("No published pricing", text)
        self.assertIn("not published", text)
        self.assertIn("unknown", text)


class DisplayNameRegistryTests(unittest.TestCase):
    def test_display_name_survives_serialization(self) -> None:
        record = _record(display_name="Alpha One")
        self.assertEqual(
            ModelRecord.from_dict(asdict(record)).display_name, "Alpha One"
        )

    def test_legacy_records_without_display_name_load_as_none(self) -> None:
        raw = asdict(_record())
        raw.pop("display_name")
        self.assertIsNone(ModelRecord.from_dict(raw).display_name)

    def test_display_name_rejects_control_characters(self) -> None:
        with self.assertRaises(ValueError):
            _record(display_name="bad\nname")

    def test_discovered_record_carries_display_name(self) -> None:
        from karox.tui import DiscoveredModel
        from karox.tui_connections import discovered_model_record

        record = discovered_model_record(
            "prov", DiscoveredModel(model_id="m1", display_name="Model One")
        )
        self.assertEqual(record.display_name, "Model One")
        self.assertEqual(record.provenance, "discovered")


if __name__ == "__main__":
    unittest.main()
