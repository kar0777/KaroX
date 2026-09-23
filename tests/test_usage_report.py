"""/usage and /economy rendering: one honest label per value, never mixed.

Pins the mandate section 9-10 contract: MEASURED for facts, ESTIMATED for
registry-rate computations, UNAVAILABLE (em dash, no digits) when nobody
reported a value; zeros the schema wrote for unreported counters never
masquerade as measurements.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from karox.usage_analytics import UsageSummary
from karox.usage_report import (
    ESTIMATED,
    MEASURED,
    UNAVAILABLE,
    build_usage_report,
    economy_measurement_lines,
    economy_status_lines,
    last_economy_event,
)


def _summary(**overrides: object) -> UsageSummary:
    values: dict[str, object] = {
        "requests": 3,
        "prompt_tokens": 1_000,
        "completion_tokens": 200,
        "total_tokens": 1_200,
    }
    values.update(overrides)
    return UsageSummary(**values)  # type: ignore[arg-type]


_ECONOMY = {
    "economy_prefix_stable_steps": 2,
    "economy_prefix_total_steps": 3,
    "economy_read_cache_hits": 1,
    "economy_batched_turns_avoided": 4,
    "economy_context_items": 7,
    "economy_context_chars_out": 900,
    "economy_reused_chars": 500,
    "economy_tool_schema_bytes_avoided": 2_000,
    "economy_continuity_statements": 3,
    "economy_cache_verdict": "REUSE",
    "economy_cache_capability": "CONFIRMED",
    "economy_context_tier": "STANDARD",
}


class DeferredToolsStatusTests(unittest.TestCase):
    """The deferral decision is per step, so its label has to be per step too."""

    def _rows(self, *, economy_mode: bool = True, **economy: object) -> list[str]:
        return economy_status_lines(
            "en", economy=dict(_ECONOMY, **economy), economy_mode=economy_mode
        )

    @staticmethod
    def _row(rows: list[str]) -> str:
        return next(row for row in rows if row.startswith("Deferred Tools"))

    def test_a_skipped_step_does_not_read_as_a_measured_zero(self) -> None:
        # Deferral is skipped when its discovery note would cost more than the
        # schema it defers, and the net counter is then 0. The global "applied"
        # label over that number claims the feature ran and measured nothing.
        row = self._row(
            self._rows(
                economy_tool_universe_applied=False,
                economy_tool_schema_bytes_saved_net=0,
            )
        )
        self.assertTrue(row.startswith("Deferred Tools: not applied on this step · "), row)
        self.assertIn("0 net schema bytes saved", row)

    def test_an_applied_step_keeps_the_applied_label(self) -> None:
        row = self._row(
            self._rows(
                economy_tool_universe_applied=True,
                economy_tool_schema_bytes_saved_net=74,
            )
        )
        self.assertTrue(row.startswith("Deferred Tools: applied · "), row)
        self.assertIn("74 net schema bytes saved", row)

    def test_a_disabled_economy_still_reports_shadow_measurement(self) -> None:
        # With economy off nothing is applied, skipped or otherwise: the row
        # keeps saying it is only measuring.
        row = self._row(
            self._rows(
                economy_mode=False,
                economy_tool_universe_applied=False,
                economy_tool_schema_bytes_saved_net=0,
            )
        )
        self.assertTrue(row.startswith("Deferred Tools: shadow-measuring · "), row)


class LastEconomyEventTests(unittest.TestCase):
    def test_picks_newest_event_with_economy_keys(self) -> None:
        usage = {
            "events": [
                {"step": 1, "economy_prefix_total_steps": 1},
                {"step": 2, "economy_prefix_total_steps": 2},
                {"step": 3},
            ]
        }
        self.assertEqual(last_economy_event(usage)["economy_prefix_total_steps"], 2)

    def test_no_events_returns_empty(self) -> None:
        self.assertEqual(last_economy_event({}), {})
        self.assertEqual(last_economy_event({"events": [{"step": 1}]}), {})


class BuildUsageReportTests(unittest.TestCase):
    def test_empty_summary_reports_no_requests(self) -> None:
        lines = build_usage_report(
            "en", runtime_line="p/m · Effort auto", summary=UsageSummary(), economy={}
        )
        self.assertIn("No measured model requests yet.", lines)

    def test_unreported_cache_zero_is_unavailable_not_zero(self) -> None:
        lines = build_usage_report(
            "en", runtime_line="p/m", summary=_summary(), economy={}
        )
        cached = next(
            line for line in lines if line.strip().startswith("cached:")
        )
        self.assertIn(UNAVAILABLE, cached)
        self.assertIn("—", cached)
        self.assertNotIn("0", cached.split(":", 1)[1])

    def test_confirmed_capability_makes_cache_zero_measured(self) -> None:
        lines = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(),
            economy={"economy_cache_capability": "CONFIRMED"},
        )
        cached = next(
            line for line in lines if line.strip().startswith("cached:")
        )
        self.assertIn(MEASURED, cached)

    def test_positive_cache_reads_are_measured(self) -> None:
        lines = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(cache_read_tokens=300),
            economy={},
        )
        cached = next(
            line for line in lines if line.strip().startswith("cached:")
        )
        self.assertIn(MEASURED, cached)
        self.assertIn("300", cached)

    def test_uncached_input_subtracts_cache_traffic(self) -> None:
        lines = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(cache_read_tokens=300, cache_write_tokens=100),
            economy={},
        )
        uncached = next(line for line in lines if "uncached:" in line)
        self.assertIn("600", uncached)

    def test_provider_cost_is_measured_and_estimate_is_labeled(self) -> None:
        with_cost = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(costs={"USD": 1.5}),
            economy={},
        )
        actual = next(line for line in with_cost if "actual:" in line)
        self.assertIn(MEASURED, actual)
        estimated = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(),
            economy={},
            estimated_cost=0.25,
        )
        actual_estimate = next(line for line in estimated if "actual:" in line)
        self.assertIn(ESTIMATED, actual_estimate)
        self.assertIn("0.2500", actual_estimate)

    def test_no_cost_information_is_unavailable(self) -> None:
        lines = build_usage_report(
            "en", runtime_line="p/m", summary=_summary(), economy={}
        )
        actual = next(line for line in lines if "actual:" in line)
        self.assertIn(UNAVAILABLE, actual)

    def test_estimated_cache_saving_is_labeled_estimated(self) -> None:
        lines = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(),
            economy={"economy_cache_saving_estimated_usd": 0.125},
        )
        saved = next(line for line in lines if "saved:" in line)
        self.assertIn(ESTIMATED, saved)

    def test_every_value_line_carries_exactly_one_label(self) -> None:
        lines = build_usage_report(
            "en",
            runtime_line="p/m",
            summary=_summary(cache_read_tokens=10, costs={"USD": 1.0}),
            economy=_ECONOMY,
        )
        for line in lines:
            count = sum(line.count(label) for label in (MEASURED, ESTIMATED, UNAVAILABLE))
            self.assertLessEqual(count, 1, line)


class EconomyLinesTests(unittest.TestCase):
    def test_absent_counters_are_unavailable(self) -> None:
        lines = economy_measurement_lines("en", {})
        rendered = "\n".join(lines)
        self.assertIn(UNAVAILABLE, rendered)
        self.assertNotIn(MEASURED + " 0", rendered)

    def test_written_zero_counter_is_measured(self) -> None:
        lines = economy_measurement_lines(
            "en", {"economy_batched_turns_avoided": 0}
        )
        turns = next(line for line in lines if "turns avoided" in line)
        self.assertIn(MEASURED, turns)
        self.assertIn("0", turns)

    def test_full_counters_render_measured(self) -> None:
        rendered = "\n".join(economy_measurement_lines("en", _ECONOMY))
        self.assertIn("prefix reuse: 2/3 · MEASURED", rendered)
        self.assertIn("REUSE (CONFIRMED)", rendered)


class EconomyStatusTests(unittest.TestCase):
    def test_off_mode_says_shadow_and_contract_line_present(self) -> None:
        lines = economy_status_lines("en", economy=_ECONOMY, economy_mode=False)
        rendered = "\n".join(lines)
        self.assertIn("Economy: OFF", rendered)
        self.assertIn("shadow-measuring", rendered)
        self.assertIn("never downgrades model, Effort, or verification", rendered)

    def test_on_mode_says_applied(self) -> None:
        lines = economy_status_lines("en", economy=_ECONOMY, economy_mode=True)
        rendered = "\n".join(lines)
        self.assertIn("Economy: ON", rendered)
        self.assertIn("applied", rendered)

    def test_unknown_subsystems_render_unavailable(self) -> None:
        lines = economy_status_lines("en", economy={}, economy_mode=False)
        rendered = "\n".join(lines)
        self.assertIn(UNAVAILABLE, rendered)
        self.assertIn("Provider Cache: — · UNAVAILABLE", rendered)

    def test_russian_localization_keeps_labels(self) -> None:
        lines = economy_status_lines("ru", economy=_ECONOMY, economy_mode=False)
        rendered = "\n".join(lines)
        self.assertIn("выключена", rendered)
        self.assertIn(MEASURED, rendered)


if __name__ == "__main__":
    unittest.main()
