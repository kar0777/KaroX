from __future__ import annotations

import io
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock, patch

from mcp.types import CallToolResult

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.affected_checks import AffectedChecksEngine, AffectedChecksError
from karox.artifacts import ArtifactStore
from karox.hot_worker import HotWorkerSupervisor
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


class AffectedSelectionSafetyTests(unittest.TestCase):
    """Selection-only regressions, without a session/runtime fixture."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "tests").mkdir()
        self.engine = AffectedChecksEngine.__new__(AffectedChecksEngine)
        self.engine.repository = self.repo
        self.engine.verification_commands = ()
        self.engine.delegate = SimpleNamespace(
            descriptors=lambda: [SimpleNamespace(name="karox.tests.run")]
        )
        self.engine._historical_tests = Mock(return_value={})

    def write_test(self, name: str, content: str = "# widget\n") -> str:
        relative = f"tests/{name}"
        (self.repo / relative).write_text(content, encoding="utf-8")
        return relative

    def assert_full(self, changed: list[str]) -> None:
        selected = self.engine.select(changed)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["arguments"], {"suite": "full"})

    def test_target_overflow_keeps_the_last_failing_test(self) -> None:
        for index in range(201):
            self.write_test(f"test_{index:03}.py")
            self.addCleanup(sys.modules.pop, f"test_{index:03}", None)
        failing = self.repo / "tests/test_200.py"
        failing.write_text(
            "# widget\nimport unittest\n"
            "class Regression(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            "        self.fail('trailing failing test must run')\n",
            encoding="utf-8",
        )
        selected = self.engine.select(["src/widget.py"])
        arguments = selected[0]["arguments"]
        if arguments["suite"] == "full":
            suite = unittest.TestLoader().discover(str(self.repo / "tests"))
        else:
            suite = unittest.TestSuite()
            for target in arguments["targets"]:
                suite.addTests(unittest.TestLoader().discover(
                    str(self.repo / "tests"), pattern=Path(target).name
                ))
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertFalse(result.wasSuccessful(), "the selected checks hid a failing test")
        self.assertEqual(arguments, {"suite": "full"})

    def test_project_config_requires_full_collection(self) -> None:
        self.write_test("test_widget.py")
        for config in ("pyproject.toml", "pytest.ini", "conftest.py", "tests/conftest.py"):
            with self.subTest(config=config):
                self.assert_full([config])

    def test_unmapped_source_not_masked_by_mapped_source_or_history(self) -> None:
        test = self.write_test("test_widget.py")
        self.engine._historical_tests.return_value = {test: 1}
        self.assert_full(["src/widget.py", "src/unmapped.py"])

    def test_shared_test_support_requires_full_collection(self) -> None:
        self.write_test("test_widget.py")
        self.assert_full(["tests/_support.py"])

    def test_suffix_named_test_is_discovered(self) -> None:
        target = self.write_test("widget_test.py")
        selected = self.engine.select([target])
        self.assertEqual(selected[0]["arguments"]["targets"], [target])

    def test_deleted_test_requires_full_collection(self) -> None:
        self.write_test("test_widget.py")
        self.assert_full(["tests/test_removed.py"])

    def test_external_test_requires_full_collection_even_without_tests_directory(self) -> None:
        (self.repo / "tests").rmdir()
        self.assert_full(["test_root.py"])

    def test_missing_test_capability_is_not_a_passing_lint_only_plan(self) -> None:
        self.write_test("test_widget.py")
        self.engine.verification_commands = (("python", "-m", "ruff", "check", "."),)
        self.engine.delegate.descriptors = lambda: [SimpleNamespace(name="karox.checks.run")]
        with self.assertRaises(AffectedChecksError) as raised:
            self.engine.select(["src/widget.py"])
        self.assertEqual(raised.exception.code, "capability_unavailable")

    def test_test_contents_are_read_once_per_selection_and_refreshed(self) -> None:
        target = self.write_test("test_behavior.py", "# first second third\n")
        original = Path.read_text
        reads: list[Path] = []

        def read(path: Path, *args: Any, **kwargs: Any) -> str:
            reads.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            selected = self.engine.select(["src/first.py", "src/second.py", "src/third.py"])
        self.assertEqual(selected[0]["arguments"]["targets"], [target])
        self.assertEqual(reads.count(self.repo / target), 1)
        (self.repo / target).write_text("# no dependency\n", encoding="utf-8")
        self.assert_full(["src/first.py"])

    def test_unreadable_test_is_not_silently_ignored(self) -> None:
        self.write_test("test_behavior.py")
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            with self.assertRaises(AffectedChecksError) as raised:
                self.engine.select(["src/widget.py"])
        self.assertEqual(raised.exception.code, "test_inspection_failed")

    def test_nul_rename_retains_old_and_new_paths(self) -> None:
        self.engine._git = Mock(return_value=(
            "R  src/new.py\0src/old.py\0 M tests/test_widget.py\0"
        ))
        self.assertEqual(self.engine._changed_files(None), [
            "src/new.py", "src/old.py", "tests/test_widget.py"
        ])

    def test_literal_arrow_in_nul_path_is_not_a_rename(self) -> None:
        self.engine._git = Mock(return_value="?? tests/test_left -> right.py\0")
        self.assertEqual(self.engine._changed_files(None), ["tests/test_left -> right.py"])


class HotWorkerAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package_name = "_karox_audit_worker"
        package = ModuleType(self.package_name)
        package.__path__ = [str(self.root)]
        self.package = package
        self.modules_patch = patch.dict(sys.modules, {self.package_name: package})
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        self.supervisor = HotWorkerSupervisor()
        self.supervisor.MODULE_NAME = f"{self.package_name}.worker"
        self.supervisor.MODULE_GROUP = (
            f"{self.package_name}.dependency", self.supervisor.MODULE_NAME,
        )
        (self.root / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.worker_path = self.root / "worker.py"
        self.worker_source = (
            f"from {self.package_name} import dependency\n"
            "VALUE = dependency.VALUE\n"
            "def validate_repo_command(*args): pass\n"
            "def execute_repo_command(*args): return {'value': VALUE}\n"
            "def execute_tests(*args): return {'value': VALUE}\n"
            "def execute_browser_command(*args): return {'value': VALUE}\n"
        )
        self.worker_path.write_text(self.worker_source, encoding="utf-8")
        self.original = self.supervisor.module()

    def test_unchanged_status_does_not_recompile_sources(self) -> None:
        with patch.object(self.supervisor, "_load_generation", side_effect=AssertionError("recompile")):
            for _ in range(3):
                self.assertEqual(self.supervisor.status()["generation"], 1)

    def test_timestamp_only_touch_does_not_reload(self) -> None:
        stat = self.worker_path.stat()
        os.utime(self.worker_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertIs(self.supervisor.module(), self.original)
        self.assertEqual(self.supervisor.status()["reload_count"], 0)

    def test_same_size_edit_with_restored_mtime_is_detected(self) -> None:
        path = self.root / "dependency.py"
        stat = path.stat()
        path.write_text("VALUE = 2\n", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.supervisor.module().VALUE, 2)

    def test_parent_package_and_worker_use_new_dependency(self) -> None:
        (self.root / "dependency.py").write_text("VALUE = 123\n", encoding="utf-8")
        candidate = self.supervisor.module()
        self.assertEqual(candidate.VALUE, 123)
        self.assertIs(self.package.worker, candidate)
        self.assertIs(self.package.dependency, sys.modules[f"{self.package_name}.dependency"])

    def test_failed_generation_is_not_recompiled_until_change_or_force(self) -> None:
        self.worker_path.write_text("def broken(:\n", encoding="utf-8")
        with patch.object(self.supervisor, "_load_generation", wraps=self.supervisor._load_generation) as load:
            self.assertIs(self.supervisor.module(), self.original)
            self.assertIs(self.supervisor.module(), self.original)
            self.assertIsNotNone(self.supervisor.status()["last_reload_error"])
            self.assertEqual(load.call_count, 1)
            with self.assertRaises(RuntimeError):
                self.supervisor.module(force=True)
            self.assertEqual(load.call_count, 2)
        self.worker_path.write_text(self.worker_source + "# repaired\n", encoding="utf-8")
        self.assertIsNot(self.supervisor.module(), self.original)
        self.assertIsNone(self.supervisor.status()["last_reload_error"])

    def test_failed_generation_restores_sys_modules_and_package_attributes(self) -> None:
        old_dependency = self.package.dependency
        (self.root / "dependency.py").write_text("VALUE = 123\n", encoding="utf-8")
        self.worker_path.write_text(self.worker_source + "raise ValueError('broken')\n", encoding="utf-8")
        self.assertIs(self.supervisor.module(), self.original)
        self.assertIs(sys.modules[self.supervisor.MODULE_NAME], self.original)
        self.assertIs(self.package.worker, self.original)
        self.assertIs(self.package.dependency, old_dependency)

    def test_missing_source_preserves_last_known_good_and_reports_error(self) -> None:
        self.worker_path.unlink()
        self.assertIs(self.supervisor.module(), self.original)
        self.assertIn("missing", self.supervisor.status()["last_reload_error"])
        with self.assertRaisesRegex(RuntimeError, "last known-good"):
            self.supervisor.module(force=True)
        self.worker_path.write_text(self.worker_source, encoding="utf-8")
        self.assertIsNone(self.supervisor.status()["last_reload_error"])

    def test_force_reload_reexecutes_unchanged_sources(self) -> None:
        self.assertIsNot(self.supervisor.module(force=True), self.original)
        self.assertEqual(self.supervisor.status()["reload_count"], 1)

    def test_source_change_between_signature_and_compile_is_not_published(self) -> None:
        self.worker_path.write_text(self.worker_source + "# first edit\n", encoding="utf-8")
        load = self.supervisor._load_generation

        def racing_load(*args: Any) -> Any:
            self.worker_path.write_text(self.worker_source + "# second edit\n", encoding="utf-8")
            return load(*args)

        with patch.object(self.supervisor, "_load_generation", side_effect=racing_load):
            self.assertIs(self.supervisor.module(), self.original)
        self.assertIs(sys.modules[self.supervisor.MODULE_NAME], self.original)
        self.assertIsNotNone(self.supervisor._last_reload_error)
        self.assertIsNot(self.supervisor.module(), self.original)
        self.assertIsNone(self.supervisor.status()["last_reload_error"])

    def test_interrupted_reload_restores_module_and_package_views(self) -> None:
        old_dependency = self.package.dependency
        self.worker_path.write_text(self.worker_source + "raise SystemExit('interrupted')\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.supervisor.module()
        self.assertIs(sys.modules[self.supervisor.MODULE_NAME], self.original)
        self.assertIs(self.package.worker, self.original)
        self.assertIs(sys.modules[f"{self.package_name}.dependency"], old_dependency)
        self.assertIs(self.package.dependency, old_dependency)

    def test_supervisor_edit_requires_restart_even_with_restored_mtime(self) -> None:
        path = self.root / "supervisor.py"
        path.write_text("before\n", encoding="utf-8")
        self.supervisor._supervisor_source_path = path
        self.supervisor._supervisor_source_sha256 = self.supervisor._digest(path)
        stat = path.stat()
        path.write_text("after!\n", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertTrue(self.supervisor.status()["bridge_restart_required"])


if __name__ == "__main__":
    unittest.main()
