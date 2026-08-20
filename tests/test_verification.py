"""Verification Autopilot tests — Phase 4."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from karox.verification import (
    LAYERS,
    AutopilotPlan,
    VerificationResult,
    build_result,
    discover_verification_commands,
    plan_autopilot,
)


class VerificationDiscoveryTests(unittest.TestCase):
    def test_node_repository_discovers_real_safe_checks_and_rejects_risky_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "test": "vitest run",
                            "verify": "npm run lint && npm run typecheck",
                            "typecheck": "tsc --noEmit",
                            "lint": "eslint .",
                            "build": "vite build",
                            "check:migrations": "tsx scripts/check-migrations.ts",
                            "ci": "npm run build && firebase deploy",
                            "test:smoke": "eslint . --fix",
                        }
                    }
                ),
                encoding="utf-8",
            )

            commands = discover_verification_commands(repository)

        self.assertEqual(
            commands,
            (
                ("npm", "test"),
                ("npm", "run", "verify"),
                ("npm", "run", "typecheck"),
                ("npm", "run", "lint"),
                ("npm", "run", "build"),
                ("npm", "run", "check:migrations"),
            ),
        )

    def test_repository_without_safe_project_checks_falls_back_to_diff_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "package.json").write_text(
                json.dumps({"scripts": {"ci": "npm publish"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                discover_verification_commands(repository),
                (("git", "diff", "--check"),),
            )


class BuildResultTests(unittest.TestCase):
    def test_pytest_exit_0_is_accepted(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest", "-q"],
            exit_code=0, stdout="3 passed", stderr="",
        )
        self.assertTrue(r.accepted)
        self.assertIn("pytest", r.accepted_reason)

    def test_pytest_exit_1_is_rejected(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest", "-q"],
            exit_code=1, stdout="1 failed", stderr="",
        )
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_reason, "exit code 1")

    def test_timeout_is_rejected(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest"],
            exit_code=None, stdout="", stderr="", timeout=True,
        )
        self.assertFalse(r.accepted)
        self.assertEqual(r.rejected_reason, "command timed out")

    def test_non_test_exit_0_without_allowlist_rejected(self) -> None:
        """Running a command is not proof of success."""
        r = build_result(
            command=["echo", "hello"],
            exit_code=0, stdout="hello", stderr="",
        )
        self.assertFalse(r.accepted)
        self.assertIn("no allowlist match", r.rejected_reason or "")

    def test_allowlisted_non_test_accepted(self) -> None:
        r = build_result(
            command=["python", "-m", "build", "--wheel"],
            exit_code=0, stdout="", stderr="",
            allowlist_match="build wheel",
        )
        self.assertTrue(r.accepted)
        self.assertIn("allowlisted", r.accepted_reason)

    def test_ruff_exit_0_accepted(self) -> None:
        r = build_result(
            command=["python", "-m", "ruff", "check", "src"],
            exit_code=0, stdout="All checks passed!", stderr="",
        )
        self.assertTrue(r.accepted)

    def test_ruff_exit_1_rejected(self) -> None:
        r = build_result(
            command=["python", "-m", "ruff", "check", "src"],
            exit_code=1, stdout="Found 1 error", stderr="",
        )
        self.assertFalse(r.accepted)

    def test_secret_redacted_from_summary(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest"],
            exit_code=0, stdout="token=super-secret-123", stderr="",
        )
        self.assertNotIn("super-secret-123", r.redacted_summary)
        self.assertIn("[REDACTED]", r.redacted_summary)

    def test_scope_and_source_recorded(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest"],
            exit_code=0, stdout="", stderr="",
            scope="broader", source="wheel",
        )
        self.assertEqual(r.scope, "broader")
        self.assertEqual(r.source, "wheel")

    def test_digests_present(self) -> None:
        r = build_result(
            command=["python", "-m", "pytest"],
            exit_code=0, stdout="output here", stderr="err",
        )
        self.assertTrue(r.stdout_digest.startswith("sha256:"))
        self.assertTrue(r.stderr_digest.startswith("sha256:"))


class AutopilotPlanTests(unittest.TestCase):
    def test_default_includes_focused_only_for_test_change(self) -> None:
        plan = plan_autopilot(
            changed_files=["tests/test_foo.py"],
            focused_tests=["tests/test_foo.py"],
        )
        self.assertIn("focused", plan.layers)
        self.assertNotIn("broader", plan.layers)

    def test_shared_module_change_includes_broader(self) -> None:
        plan = plan_autopilot(
            changed_files=["src/karox/core.py"],
            focused_tests=["tests/test_core.py"],
        )
        self.assertIn("broader", plan.layers)
        self.assertIn("related_regression", plan.layers)

    def test_full_suite_requested_includes_all_layers(self) -> None:
        plan = plan_autopilot(
            changed_files=["src/karox/core.py"],
            full_suite_requested=True,
        )
        self.assertIn("release_gates", plan.layers)
        self.assertIn("broader", plan.layers)

    def test_no_changes_skips_focused(self) -> None:
        plan = plan_autopilot()
        self.assertNotIn("focused", plan.layers)
        self.assertIn("focused", plan.skipped)

    def test_layer_order_is_canonical(self) -> None:
        self.assertEqual(LAYERS, ("focused", "related_regression", "broader", "release_gates"))


if __name__ == "__main__":
    unittest.main()
