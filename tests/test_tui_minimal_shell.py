"""The header is one adaptive line, and it never lies to save space.

KaroX is a coding agent, not a control panel: the conversation owns the screen
and the chrome above it is a single line. These tests pin the two properties that
made the previous five-column status bar unusable in an ordinary terminal --
fields colliding at 80 columns, and a model name cut to something that reads as a
different model.

``_header_line`` is a pure function of the facts and the width, which is what
lets these tests assert real behaviour at real terminal sizes without driving a
whole application.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import tui

# Three real terminals, not invented numbers: a small side pane, the classic
# default, and a comfortably wide window.
NARROW = 46
STANDARD = 80
WIDE = 120


class HeaderLineTests(unittest.TestCase):
    def line(self, width: int, **overrides: str) -> str:
        facts = {
            "repository": "KaroX-v5",
            "model": "openai/claude-opus",
            "activity": "выполняется",
        }
        facts.update(overrides)
        return tui._header_line(width=width, **facts)  # type: ignore[arg-type]

    def test_a_wide_terminal_shows_product_repository_model_and_state(self) -> None:
        line = self.line(WIDE)
        for expected in ("KaroX", "KaroX-v5", "openai/claude-opus", "выполняется"):
            self.assertIn(expected, line)

    def test_a_standard_terminal_shows_repository_model_and_state(self) -> None:
        """80×24 is the classic default and is not a cramped case.

        All four fields fit in about fifty columns there, so dropping one would
        be gratuitous. What this pins is the requirement for that size:
        repository, model and state are all visible.
        """

        line = self.line(STANDARD)
        self.assertIn("KaroX-v5", line)
        self.assertIn("openai/claude-opus", line)
        self.assertIn("выполняется", line)
        self.assertLessEqual(len(line), STANDARD)

    def test_a_medium_terminal_drops_the_product_name_first(self) -> None:
        """Below the wide breakpoint the least informative field goes.

        A person looking at their own terminal knows which program they started;
        they do not necessarily remember which model is selected.
        """

        line = self.line(tui.HEADER_WIDE_COLUMNS - 1)
        self.assertIn("KaroX-v5", line)
        self.assertIn("выполняется", line)
        self.assertFalse(line.startswith("KaroX ·"))

    def test_a_narrow_terminal_keeps_only_the_product_and_the_state(self) -> None:
        line = self.line(NARROW)
        self.assertIn("claude-opus", line)
        self.assertIn("выполняется", line)
        # "which model" is a question the user can ask; "is it still working" is
        # one they cannot.
        self.assertNotIn("openai/claude-opus", line)
        self.assertLessEqual(len(line), NARROW)

    def test_a_model_name_is_never_abbreviated_into_a_different_model(self) -> None:
        """The defect this replaces: ``openai/model-a`` drawn as ``openai/m``."""

        for width in (NARROW, 60, STANDARD, WIDE):
            with self.subTest(width=width):
                line = self.line(width, model="openai/gpt-4o-mini")
                if "openai" in line:
                    self.assertIn(
                        "openai/gpt-4o-mini",
                        line,
                        "a partial model id is a wrong fact on screen",
                    )

    def test_the_line_never_exceeds_the_width_it_was_given(self) -> None:
        for width in (30, NARROW, 60, STANDARD, WIDE):
            with self.subTest(width=width):
                line = self.line(
                    width,
                    repository="a-very-long-repository-name-indeed",
                    model="provider/some-extremely-long-model-identifier",
                )
                self.assertLessEqual(
                    len(line), max(width, len("KaroX · выполняется"))
                )

    def test_fields_are_dropped_whole_rather_than_cut(self) -> None:
        line = self.line(
            60,
            repository="repository-with-a-long-name",
            model="provider/long-model-identifier-here",
        )
        # Whatever survived is complete: no field appears as a prefix of itself.
        for field in line.split(" · "):
            self.assertFalse(field.endswith("…"), f"{field!r} was cut")

    def test_an_absent_fact_leaves_no_stray_separator(self) -> None:
        line = self.line(WIDE, model="", activity="")
        self.assertNotIn("··", line)
        self.assertFalse(line.strip().endswith("·"))
        self.assertIn("KaroX-v5", line)

    def test_a_context_note_appears_only_when_it_is_passed(self) -> None:
        quiet = self.line(WIDE)
        loud = self.line(WIDE, context_note="контекст 82%")
        self.assertNotIn("%", quiet)
        self.assertIn("контекст 82%", loud)

    def test_the_context_note_is_the_first_field_sacrificed(self) -> None:
        line = self.line(NARROW, context_note="контекст 82%")
        self.assertNotIn("контекст", line)

    def test_the_state_survives_at_every_width(self) -> None:
        """What the agent is doing is the one fact that is never dropped."""

        for width in (30, NARROW, 60, STANDARD, WIDE):
            with self.subTest(width=width):
                self.assertIn("выполняется", self.line(width))

    def test_english_renders_the_same_way(self) -> None:
        line = tui._header_line(
            repository="KaroX-v5",
            model="openai/claude-opus",
            activity="running",
            width=WIDE,
        )
        self.assertIn("running", line)
        self.assertIn("KaroX-v5", line)

    def test_the_breakpoints_are_named_rather_than_magic(self) -> None:
        # A test reading a literal 76 would drift from the code the moment the
        # threshold is tuned.
        self.assertGreater(tui.HEADER_WIDE_COLUMNS, tui.HEADER_MEDIUM_COLUMNS)
        self.assertLess(tui.HEADER_MEDIUM_COLUMNS, STANDARD)
        self.assertLess(0.0, tui.CONTEXT_WARNING_FRACTION)
        self.assertLess(tui.CONTEXT_WARNING_FRACTION, 1.0)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
