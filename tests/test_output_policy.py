"""P1.4 output policy: progressive disclosure without losing failures."""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.output_policy import (
    OutputMode,
    TurnReport,
    measure_output,
    render_turn_report,
    resolve_mode,
)

_GREEN = TurnReport(
    result="economy stack wired into the native turn",
    verification="2859 passed, 5 skipped in 13:33",
    evidence=tuple(f"evidence line {index}: cache hit ratio detail" for index in range(12)),
    explanations=("batched reads to avoid repeated round trips",),
    details_ref="artifact art-0123456789ab",
)

_RED = TurnReport(
    result="wheel built",
    verification="focused suite",
    warnings=("coverage below threshold on proxy_server",),
    failures=("tests/test_release_gates.py::test_wheel_contents FAILED",),
    evidence=("first failure at line 88",),
    details_ref="artifact art-fedcba987654",
)


class AdaptiveModeTests(unittest.TestCase):
    def test_green_report_resolves_to_concise(self) -> None:
        self.assertIs(resolve_mode(_GREEN, OutputMode.ADAPTIVE), OutputMode.CONCISE)

    def test_failures_escalate_to_standard(self) -> None:
        self.assertIs(resolve_mode(_RED, OutputMode.ADAPTIVE), OutputMode.STANDARD)

    def test_explicit_modes_are_not_overridden(self) -> None:
        self.assertIs(resolve_mode(_RED, OutputMode.DETAILED), OutputMode.DETAILED)


class SafetyInvariantTests(unittest.TestCase):
    def test_failures_and_warnings_survive_every_mode(self) -> None:
        for mode in OutputMode:
            with self.subTest(mode=mode.value):
                text = render_turn_report(_RED, mode)
                self.assertIn("FAILED", text)
                self.assertIn("coverage below threshold", text)

    def test_failures_render_first(self) -> None:
        text = render_turn_report(_RED, OutputMode.CONCISE)
        self.assertTrue(text.splitlines()[0].endswith("FAILED"))

    def test_details_reference_present_outside_detailed(self) -> None:
        for mode in (OutputMode.CONCISE, OutputMode.STANDARD, OutputMode.LEARNING):
            with self.subTest(mode=mode.value):
                self.assertIn("[Details]", render_turn_report(_GREEN, mode))
        self.assertNotIn("[Details]", render_turn_report(_GREEN, OutputMode.DETAILED))


class ProgressiveDisclosureTests(unittest.TestCase):
    def test_concise_hides_evidence_standard_bounds_it(self) -> None:
        concise = render_turn_report(_GREEN, OutputMode.CONCISE)
        standard = render_turn_report(_GREEN, OutputMode.STANDARD)
        detailed = render_turn_report(_GREEN, OutputMode.DETAILED)
        self.assertNotIn("evidence line 0", concise)
        self.assertIn("evidence line 0", standard)
        self.assertNotIn("evidence line 11", standard)
        self.assertIn("(7 more in details)", standard)
        self.assertIn("evidence line 11", detailed)

    def test_learning_adds_why_lines(self) -> None:
        learning = render_turn_report(_GREEN, OutputMode.LEARNING)
        standard = render_turn_report(_GREEN, OutputMode.STANDARD)
        self.assertIn("why:", learning)
        self.assertNotIn("why:", standard)

    def test_rendering_is_deterministic(self) -> None:
        for mode in OutputMode:
            self.assertEqual(
                render_turn_report(_GREEN, mode), render_turn_report(_GREEN, mode)
            )


class MeasurementTests(unittest.TestCase):
    def test_concise_saves_at_least_forty_percent_on_verbose_reports(self) -> None:
        measurement = measure_output(_GREEN)
        self.assertGreater(measurement.concise_saving_ratio, 0.4)

    def test_measurement_covers_every_mode(self) -> None:
        measurement = measure_output(_GREEN)
        self.assertEqual(
            sorted(measurement.chars_by_mode), sorted(m.value for m in OutputMode)
        )

    def test_mapping_roundtrip_from_envelope_payload(self) -> None:
        report = TurnReport.from_mapping(
            {
                "result": "done",
                "verification": "tests green",
                "warnings": ["one warning"],
                "failures": [],
                "evidence": ["a", "b"],
                "details_ref": "artifact art-1",
            }
        )
        text = render_turn_report(report, OutputMode.ADAPTIVE)
        self.assertIn("done", text)
        self.assertIn("one warning", text)


if __name__ == "__main__":
    unittest.main()
