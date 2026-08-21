"""Agent modes are real behavior, not an enum in /status.

The policy table is the enforcement surface: these tests prove the modes
differ where the product promises they differ (mutation default, artifact,
prompt delta) and that the mode vocabulary stays orthogonal to the effort
ladder.
"""

import unittest

from karox import agent_modes
from karox.agent_modes import (
    DEFAULT_MODE,
    MODES,
    ModeError,
    mode_display_name,
    mode_policy,
    mode_prompt_delta,
    normalize_mode,
)


class NormalizeModeTests(unittest.TestCase):
    def test_canonical_ids_pass_through(self) -> None:
        for mode in MODES:
            self.assertEqual(normalize_mode(mode), mode)

    def test_case_whitespace_and_slash_are_tolerated(self) -> None:
        self.assertEqual(normalize_mode("  Build "), "build")
        self.assertEqual(normalize_mode("/plan"), "plan")
        self.assertEqual(normalize_mode("IDEATE"), "ideate")

    def test_short_aliases(self) -> None:
        self.assertEqual(normalize_mode("b"), "build")
        self.assertEqual(normalize_mode("p"), "plan")
        self.assertEqual(normalize_mode("i"), "ideate")

    def test_junk_raises_mode_error(self) -> None:
        for junk in ("exactly", "", None, 7, "brainstorm"):
            with self.assertRaises(ModeError):
                normalize_mode(junk)

    def test_default_mode_is_build(self) -> None:
        self.assertEqual(DEFAULT_MODE, "build")
        self.assertIn(DEFAULT_MODE, MODES)


class ModePolicyTests(unittest.TestCase):
    def test_only_build_mutates_by_default(self) -> None:
        self.assertTrue(mode_policy("build").mutates_by_default)
        self.assertFalse(mode_policy("plan").mutates_by_default)
        self.assertFalse(mode_policy("ideate").mutates_by_default)

    def test_artifact_expectations_differ(self) -> None:
        self.assertIsNone(mode_policy("build").default_artifact)
        self.assertEqual(mode_policy("plan").default_artifact, "plan")
        self.assertEqual(mode_policy("ideate").default_artifact, "concept")

    def test_prompt_deltas_are_distinct_compact_and_labeled(self) -> None:
        deltas = {mode: mode_prompt_delta(mode) for mode in MODES}
        self.assertEqual(len(set(deltas.values())), len(MODES))
        for mode, delta in deltas.items():
            self.assertLess(len(delta), 1200, mode)
            self.assertIn(f"MODE: {mode.upper()}", delta)

    def test_plan_and_ideate_deltas_forbid_silent_implementation(self) -> None:
        self.assertIn("Do not mutate production code", mode_prompt_delta("plan"))
        self.assertIn("Do not implement until the user", mode_prompt_delta("ideate"))

    def test_display_names_cover_both_languages(self) -> None:
        for mode in MODES:
            self.assertTrue(mode_display_name(mode, "ru"))
            self.assertTrue(mode_display_name(mode, "en"))
        self.assertEqual(mode_display_name("build", "en"), "Build")

    def test_third_mode_is_not_named_exactly(self) -> None:
        for mode in MODES:
            self.assertNotEqual(mode_display_name(mode, "en").lower(), "exactly")


class OrthogonalityTests(unittest.TestCase):
    def test_modes_module_does_not_import_effort(self) -> None:
        """Mode and Effort are orthogonal axes; neither owns the other."""

        import karox.effort as effort

        self.assertFalse(hasattr(agent_modes, "effort"))
        self.assertFalse(hasattr(effort, "agent_modes"))

    def test_mode_vocabulary_does_not_collide_with_effort_levels(self) -> None:
        from karox.effort import EFFORT_LEVELS

        self.assertFalse(set(MODES) & set(EFFORT_LEVELS))


if __name__ == "__main__":
    unittest.main()
