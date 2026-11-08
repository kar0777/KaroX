"""Offline integrity tests; no provider requests or benchmark agents."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("hard20_reconcile_live", HERE / "hard20_reconcile_live.py")
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hard20-reconcile-test-")
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(mod, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.result = (self.root / "hard20-max-runs" / "hard20-test-a" /
                       mod.TASK / "karox5" / "result.json")
        self.result.parent.mkdir(parents=True)
        self.result.write_text(json.dumps({
            "run_id": "real-dev-id",
            "task_id": mod.TASK,
            "arm": "karox5",
            "agent_exit_code": 1,
            "harness_config_untampered": True,
            "quality_classification": {"status": "ARTIFACT_FAIL"},
            "terminal_events": {"terminal_seen": True, "usage": {"prompt": 100}},
            "grader_exit_code": 2,
        }), encoding="utf-8")

    def host_grade(self, *, clean: bool, grade: str = "PASS", valid_hash: bool = True) -> None:
        ledger = self.root / "hard20-max-runs" / "HARD20_HOST_REGRADE_LEDGER.jsonl"
        row = {
            "label": "hard20-test-a",
            "task_id": mod.TASK,
            "arm": "karox5",
            "source_cli_result_sha256": (mod.digest(self.result) if valid_hash else "0" * 64),
            "external_grade": grade,
            "clean_user_success": clean,
        }
        ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def test_infrastructure_grade_does_not_mean_quality_fail(self) -> None:
        row = mod.reconcile()["rows"][0]
        self.assertEqual(row["classification"], "INFRA_OR_NOT_GRADED")
        self.assertEqual(row["external_grade"], "UNJUDGED")
        self.assertFalse(row["clean_cli_success"])

    def test_patch_pass_must_not_be_claimed_clean(self) -> None:
        self.host_grade(clean=False)
        row = mod.reconcile()["rows"][0]
        self.assertEqual(row["classification"], "PATCH_PASS_BUT_CLI_INCOMPLETE")
        self.assertEqual(row["raw_grader_exit"], 2)
        self.assertFalse(row["clean_cli_success"])

    def test_clean_is_a_separate_independent_judgment(self) -> None:
        data = json.loads(self.result.read_text(encoding="utf-8"))
        data["agent_exit_code"] = 0
        self.result.write_text(json.dumps(data), encoding="utf-8")
        self.host_grade(clean=True)
        row = mod.reconcile()["rows"][0]
        self.assertEqual(row["classification"], "CLEAN_PASS")

    def test_host_flag_does_not_override_failed_cli(self) -> None:
        self.host_grade(clean=True)
        row = mod.reconcile()["rows"][0]
        self.assertEqual(row["classification"], "PATCH_PASS_BUT_CLI_INCOMPLETE")
        self.assertFalse(row["clean_cli_success"])

    def test_host_flag_does_not_override_failed_grade(self) -> None:
        self.host_grade(clean=True, grade="FAIL")
        row = mod.reconcile()["rows"][0]
        self.assertEqual(row["classification"], "QUALITY_FAIL")
        self.assertFalse(row["clean_cli_success"])

    def test_changed_result_invalidates_host_evidence(self) -> None:
        self.host_grade(clean=True, valid_hash=False)
        with self.assertRaisesRegex(ValueError, "changed"):
            mod.reconcile()


if __name__ == "__main__":
    unittest.main()
