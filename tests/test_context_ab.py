from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.context_ab import (
    ContextBenchmarkCase,
    compare_context_case,
    run_context_ab,
    summarize_context_ab,
)


class ContextABTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp.name) / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "src").mkdir()
        (self.repository / "tests").mkdir()
        (self.repository / "src" / "feature.py").write_text(
            "from .helper import normalize\n\n"
            "WIDGET_SENTINEL = 'widget-normalization-v1'\n\n"
            "def handle(value: str) -> str:\n"
            "    return normalize(value)\n",
            encoding="utf-8",
        )
        # No task words here. This file should be found through the import edge,
        # not because the benchmark goal happens to repeat its name.
        (self.repository / "src" / "helper.py").write_text(
            "def normalize(value: str) -> str:\n"
            "    return value.strip().lower()\n",
            encoding="utf-8",
        )
        # Reverse edge: api imports the root implementation but also avoids the
        # WIDGET_SENTINEL token, so depth=1 has to discover it as a caller.
        (self.repository / "src" / "api.py").write_text(
            "import src.feature as backend\n\n"
            "def dispatch(value: str) -> str:\n"
            "    return backend.handle(value)\n",
            encoding="utf-8",
        )
        (self.repository / "tests" / "test_feature.py").write_text(
            "from src.feature import handle\n\n"
            "def test_handle():\n"
            "    assert handle(' A ') == 'a'\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def case(self) -> ContextBenchmarkCase:
        return ContextBenchmarkCase(
            "widget-routing",
            "fix the WIDGET_SENTINEL regression and its tests",
            expected_implementation=(
                "src/feature.py",
                "src/helper.py",
                "src/api.py",
            ),
            expected_tests=("tests/test_feature.py",),
        )

    def test_recursive_arm_adds_real_dependency_neighbors_without_extra_scan_cache(self) -> None:
        result = compare_context_case(self.repository, self.case())

        self.assertFalse(result.root.cache_hit)
        self.assertFalse(result.recursive.cache_hit)
        self.assertTrue(result.recursive.recursive_enabled)
        self.assertGreater(result.recursive.recursive_edge_count, 0)
        self.assertIn("src/helper.py", result.added_implementation)
        self.assertIn("src/api.py", result.added_implementation, result)
        self.assertGreaterEqual(
            result.recursive.implementation_score.hits,
            result.root.implementation_score.hits,
        )
        self.assertGreater(result.prompt_delta_chars, 0)

    def test_summary_reports_quality_and_overhead_deltas(self) -> None:
        results = run_context_ab(self.repository, [self.case()])
        summary = summarize_context_ab(results)

        self.assertEqual(summary.cases, 1)
        self.assertEqual(summary.recursive_enabled_cases, 1)
        self.assertGreaterEqual(summary.implementation_hits_delta, 1)
        self.assertGreater(summary.mean_implementation_f1_delta, 0)
        self.assertEqual(summary.quality_wins, 1)
        self.assertEqual(summary.quality_losses, 0)
        self.assertGreater(summary.mean_prompt_delta_chars, 0)

    def test_empty_summary_is_well_defined(self) -> None:
        summary = summarize_context_ab([])
        self.assertEqual(summary.cases, 0)
        self.assertEqual(summary.mean_duration_delta_ms, 0.0)
        self.assertEqual(summary.mean_prompt_delta_chars, 0.0)


if __name__ == "__main__":
    unittest.main()
