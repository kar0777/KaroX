from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from mcp.types import CallToolResult

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.affected_checks import AffectedChecksEngine, AffectedChecksError
from karox.artifacts import ArtifactStore
from karox.models import AccessProfile
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import TaskStateStore


class AffectedDelegate:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.calls: list[tuple[str, dict[str, Any], Optional[str]]] = []
        self.failure_text: Optional[str] = None
        self.success_when_fixed = False
        self.names = {
            "karox.tests.run",
            "karox.checks.run",
            "karox.repo.command",
        }

    def descriptors(self) -> list[Any]:
        return [SimpleNamespace(name=name) for name in sorted(self.names)]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any] | CallToolResult:
        del deadline_seconds
        self.calls.append((tool_name, dict(arguments), idempotency_key))
        if tool_name == "karox.repo.command":
            changed: list[str] = []
            payload = arguments.get("payload", {})
            for operation in payload.get("operations", []):
                if operation.get("op") != "write":
                    continue
                relative = str(operation["path"])
                path = self.repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(operation["content"]), encoding="utf-8")
                changed.append(relative)
            return {"ok": True, "changed_files": changed}
        if tool_name == "karox.checks.run":
            return {
                "ok": True,
                "exit_code": 0,
                "stdout": "lint passed\n",
                "stderr": "",
                "timed_out": False,
            }
        if tool_name == "karox.tests.run":
            content = (self.repository / "src" / "karox" / "widget.py").read_text(
                encoding="utf-8"
            )
            if self.success_when_fixed and "fixed = True" in content:
                return {
                    "ok": True,
                    "exit_code": 0,
                    "stdout": "1 passed\n",
                    "stderr": "",
                    "timed_out": False,
                }
            if self.failure_text is None:
                return {
                    "ok": True,
                    "exit_code": 0,
                    "stdout": "1 passed\n",
                    "stderr": "",
                    "timed_out": False,
                }
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": f"FAILED tests/test_widget.py::test_widget - {self.failure_text}\n",
                "stderr": "",
                "timed_out": False,
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class AffectedChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / "src" / "karox").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "karox" / "widget.py").write_text(
            "def widget():\n    return 1\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_widget.py").write_text(
            "from karox.widget import widget\n\n"
            "def test_widget():\n"
            "    assert widget() == 1\n",
            encoding="utf-8",
        )
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Run affected checks",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        self.artifacts = ArtifactStore("session-a")
        self.context = RepositoryContextEngine(
            self.repo,
            self.artifacts,
            policy_profile="workspace_write",
        )
        self.delegate = AffectedDelegate(self.repo)
        self.engine = AffectedChecksEngine(
            repository=self.repo,
            session_id="session-a",
            connection_id="chat-a",
            delegate=self.delegate,
            verification_commands=(("python", "-m", "ruff", "check", "src", "tests"),),
            artifacts=self.artifacts,
            repo_context=self.context,
            task_states=TaskStateStore(self.sessions),
            lease_store=RepositoryLeaseStore(root / "repository-leases"),
        )
        self.changed = ["src/karox/widget.py"]

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    def test_auto_detected_tracked_path_keeps_first_character(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "."],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=KaroX Test",
                "-c",
                "user.email=karox@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        path = self.repo / "src" / "karox" / "widget.py"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nTRACKED = True\n",
            encoding="utf-8",
        )
        changed = self.engine._changed_files(None)
        self.assertIn("src/karox/widget.py", changed)
        self.assertNotIn("rc/karox/widget.py", changed)

    def test_run_checkpoints_only_the_selected_workstream(self) -> None:
        self.engine.task_states.bootstrap(
            "session-a",
            {},
            workstream_id="frontend",
        )
        self.engine.task_states.bootstrap(
            "session-a",
            {},
            workstream_id="backend",
        )
        backend_before = self.engine.task_states.load(
            "session-a",
            workstream_id="backend",
        )

        result = self.engine.run(
            {"mode": "current", "changed_files": self.changed},
            "affected-frontend",
            workstream_id="frontend",
        )

        frontend = self.engine.task_states.load(
            "session-a",
            workstream_id="frontend",
        )
        backend_after = self.engine.task_states.load(
            "session-a",
            workstream_id="backend",
        )
        self.assertEqual(result["workstream_id"], "frontend")
        self.assertIn("checks_executed", frontend.facts)
        self.assertNotIn("checks_executed", backend_after.facts)
        self.assertEqual(backend_after.revision, backend_before.revision)
        self.assertIsNone(self.engine.task_states.load_optional("session-a"))

    def test_selection_uses_name_import_and_approved_lint(self) -> None:
        selected = self.engine.select(self.changed)
        by_tool = {item["tool"]: item for item in selected}
        self.assertIn("karox.tests.run", by_tool)
        self.assertEqual(
            by_tool["karox.tests.run"]["arguments"]["targets"],
            ["tests/test_widget.py"],
        )
        reasons = by_tool["karox.tests.run"]["reasons"]
        self.assertTrue(any(reason.startswith("name_match:") for reason in reasons))
        self.assertIn("karox.checks.run", by_tool)
        self.assertIn("approved_lint_for_changed_language", by_tool["karox.checks.run"]["reasons"])

    def test_failure_without_controlled_baseline_is_unknown(self) -> None:
        self.delegate.failure_text = "assert 1 == 2"
        result = self.engine.run(
            {"mode": "current", "changed_files": self.changed},
            "affected-unknown",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"]["status"], "unknown")
        self.assertEqual(result["classification"]["evidence"], "no_controlled_baseline")
        self.assertIsNotNone(result["first_failure"])
        self.assertTrue(self.artifacts.exists(result["artifact_id"]))

    def test_same_failure_from_controlled_baseline_is_pre_existing(self) -> None:
        self.delegate.failure_text = "assert 1 == 2"
        baseline = self.engine.run(
            {"mode": "baseline", "changed_files": self.changed},
            "affected-baseline-preexisting",
        )
        current = self.engine.run(
            {
                "mode": "current",
                "changed_files": self.changed,
                "baseline_artifact_id": baseline["baseline_artifact_id"],
            },
            "affected-current-preexisting",
        )
        self.assertEqual(current["classification"]["status"], "pre_existing")
        self.assertEqual(
            current["classification"]["evidence"],
            "same_failure_signature_in_controlled_baseline",
        )

    def test_failure_absent_from_passing_baseline_is_new(self) -> None:
        baseline = self.engine.run(
            {"mode": "baseline", "changed_files": self.changed},
            "affected-baseline-pass",
        )
        self.assertTrue(baseline["ok"])
        self.delegate.failure_text = "new regression"
        current = self.engine.run(
            {
                "mode": "current",
                "changed_files": self.changed,
                "baseline_artifact_id": baseline["baseline_artifact_id"],
            },
            "affected-current-new",
        )
        self.assertEqual(current["classification"]["status"], "new")
        self.assertEqual(
            current["classification"]["evidence"],
            "failure_absent_from_same_check_plan_baseline",
        )

    def test_bounded_fix_candidate_runs_checks_and_succeeds(self) -> None:
        self.delegate.failure_text = "widget is broken"
        self.delegate.success_when_fixed = True
        result = self.engine.run(
            {
                "mode": "current",
                "changed_files": self.changed,
                "fix_attempts": [
                    {
                        "command": {
                            "action": "batch",
                            "payload": {
                                "operations": [
                                    {
                                        "op": "write",
                                        "path": "src/karox/widget.py",
                                        "content": "fixed = True\n\ndef widget():\n    return 1\n",
                                    }
                                ]
                            },
                        },
                        "expected_paths": ["src/karox/widget.py"],
                    }
                ],
            },
            "affected-fix-success",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["fix_attempts"]), 1)
        self.assertTrue(result["fix_attempts"][0]["ok"])
        self.assertIn(
            "fixed = True",
            (self.repo / "src" / "karox" / "widget.py").read_text(encoding="utf-8"),
        )

    def test_identical_failure_stops_before_second_candidate(self) -> None:
        self.delegate.failure_text = "same failure"
        candidates = []
        for index in (1, 2):
            candidates.append(
                {
                    "command": {
                        "action": "batch",
                        "payload": {
                            "operations": [
                                {
                                    "op": "write",
                                    "path": "src/karox/widget.py",
                                    "content": f"attempt = {index}\n",
                                }
                            ]
                        },
                    },
                    "expected_paths": ["src/karox/widget.py"],
                }
            )
        result = self.engine.run(
            {
                "mode": "current",
                "changed_files": self.changed,
                "fix_attempts": candidates,
            },
            "affected-identical-stop",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["fix_attempts"]), 1)
        self.assertEqual(result["fix_attempts"][0]["stopped_reason"], "identical_failure")
        self.assertEqual(
            len([call for call in self.delegate.calls if call[0] == "karox.repo.command"]),
            1,
        )

    def test_fix_scope_growth_is_rejected_before_patch(self) -> None:
        self.delegate.failure_text = "broken"
        with self.assertRaises(AffectedChecksError) as caught:
            self.engine.run(
                {
                    "mode": "current",
                    "changed_files": self.changed,
                    "fix_attempts": [
                        {
                            "command": {
                                "action": "batch",
                                "payload": {
                                    "operations": [
                                        {
                                            "op": "write",
                                            "path": "unrelated.py",
                                            "content": "x = 1\n",
                                        }
                                    ]
                                },
                            },
                            "expected_paths": ["unrelated.py"],
                        }
                    ],
                },
                "affected-scope-growth",
            )
        self.assertEqual(caught.exception.code, "scope_growth")
        self.assertFalse((self.repo / "unrelated.py").exists())

    def test_baseline_plan_mismatch_is_rejected(self) -> None:
        baseline = self.engine.run(
            {"mode": "baseline", "changed_files": self.changed},
            "affected-baseline-mismatch",
        )
        with self.assertRaises(AffectedChecksError) as caught:
            self.engine.run(
                {
                    "mode": "current",
                    "changed_files": ["README.md"],
                    "baseline_artifact_id": baseline["baseline_artifact_id"],
                },
                "affected-current-mismatch",
            )
        self.assertEqual(caught.exception.code, "baseline_mismatch")

    def test_transient_delegate_failure_retries_once_with_same_idempotency(self) -> None:
        class TransientDelegate(AffectedDelegate):
            def __init__(self, repository: Path) -> None:
                super().__init__(repository)
                self.failed_once = False

            def execute(
                self,
                tool_name: str,
                arguments: dict[str, Any],
                *,
                idempotency_key: Optional[str] = None,
                deadline_seconds: float = 30.0,
            ) -> dict[str, Any] | CallToolResult:
                if tool_name == "karox.tests.run" and not self.failed_once:
                    self.failed_once = True
                    self.calls.append((tool_name, dict(arguments), idempotency_key))
                    raise TimeoutError("transport timed out")
                return super().execute(
                    tool_name,
                    arguments,
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )

        delegate = TransientDelegate(self.repo)
        self.engine.delegate = delegate
        result = self.engine.run(
            {"mode": "current", "changed_files": self.changed},
            "affected-transient-retry",
        )

        self.assertTrue(result["ok"])
        calls = [call for call in delegate.calls if call[0] == "karox.tests.run"]
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(calls[0][2])
        self.assertEqual(calls[0][2], calls[1][2])
        test_result = next(
            item for item in result["results"] if item["tool"] == "karox.tests.run"
        )
        self.assertTrue(test_result["compact"]["ok"])

    def test_non_retryable_delegate_failure_is_not_replayed(self) -> None:
        class InvalidDelegate(AffectedDelegate):
            def execute(
                self,
                tool_name: str,
                arguments: dict[str, Any],
                *,
                idempotency_key: Optional[str] = None,
                deadline_seconds: float = 30.0,
            ) -> dict[str, Any] | CallToolResult:
                if tool_name == "karox.tests.run":
                    self.calls.append((tool_name, dict(arguments), idempotency_key))
                    raise ValueError("bad request")
                return super().execute(
                    tool_name,
                    arguments,
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )

        delegate = InvalidDelegate(self.repo)
        self.engine.delegate = delegate
        result = self.engine.run(
            {"mode": "current", "changed_files": self.changed},
            "affected-nonretryable",
        )

        self.assertFalse(result["ok"])
        calls = [call for call in delegate.calls if call[0] == "karox.tests.run"]
        self.assertEqual(len(calls), 1)
        test_result = next(
            item for item in result["results"] if item["tool"] == "karox.tests.run"
        )
        self.assertEqual(test_result["compact"]["error_code"], "check_execution_failed")


if __name__ == "__main__":
    unittest.main()
