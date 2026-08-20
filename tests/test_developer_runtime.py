"""Regression coverage for stable hot-reload developer commands."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.core_tools import ExtendedCoreRuntime
from karox.hot_worker import HotWorkerSupervisor
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile
from karox.policy import CapabilityPolicy
from karox.sessions import SessionStore
from karox.workspace_transaction import WorkspaceTransaction
from karox.workspace_worker import _test_files, execute_tests


class StableDeveloperCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "developer runtime",
            AccessProfile.WORKSPACE_WRITE,
            session_id="developer-runtime",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bridge(self, *tools: str) -> CoreToolBridge:
        return CoreToolBridge(
            self.repository,
            self.sessions,
            "developer-runtime",
            list(tools),
            audit_path=self.root / "audit.jsonl",
        )

    def test_apply_patch_changes_multiple_files_atomically(self) -> None:
        bridge = self._bridge("karox.repo.command")
        digest = hashlib.sha256(
            (self.repository / "sample.txt").read_bytes()
        ).hexdigest()
        patch = """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-before
+after
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+line one
+line two
"""
        result = bridge.execute(
            "karox.repo.command",
            {
                "action": "apply_patch",
                "payload": {
                    "patch": patch,
                    "expected_sha256": {"sample.txt": digest},
                },
            },
            idempotency_key="patch-multiple",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["worker_protocol_version"], 1)
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "after\n",
        )
        self.assertEqual(
            (self.repository / "new.txt").read_text(encoding="utf-8"),
            "line one\nline two\n",
        )
        self.assertEqual(
            result["data"]["changed_files"],
            ["new.txt", "sample.txt"],
        )

    def test_patch_conflict_writes_nothing(self) -> None:
        bridge = self._bridge("karox.repo.command")
        patch = """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-not-the-current-line
+after
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,1 @@
+created
"""
        with self.assertRaisesRegex(Exception, "patch conflict"):
            bridge.execute(
                "karox.repo.command",
                {"action": "apply_patch", "payload": {"patch": patch}},
                idempotency_key="patch-conflict",
            )
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        self.assertFalse((self.repository / "new.txt").exists())

    def test_patch_preserves_no_final_newline_marker(self) -> None:
        (self.repository / "sample.txt").write_text("before", encoding="utf-8")
        bridge = self._bridge("karox.repo.command")
        patch = """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-before
\\ No newline at end of file
+after
\\ No newline at end of file
"""
        result = bridge.execute(
            "karox.repo.command",
            {"action": "apply_patch", "payload": {"patch": patch}},
            idempotency_key="patch-no-final-newline",
        )
        self.assertTrue(result["ok"])
        self.assertEqual((self.repository / "sample.txt").read_bytes(), b"after")

    def test_multiple_patch_sections_use_virtual_file_state(self) -> None:
        bridge = self._bridge("karox.repo.command")
        patch = """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-before
+middle
--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-middle
+after
"""
        result = bridge.execute(
            "karox.repo.command",
            {"action": "apply_patch", "payload": {"patch": patch}},
            idempotency_key="patch-repeated-file",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "after\n",
        )

    def test_patch_create_and_rename_conflicts_write_nothing(self) -> None:
        bridge = self._bridge("karox.repo.command")
        create_existing = """--- /dev/null
+++ b/sample.txt
@@ -0,0 +1,1 @@
+replacement
"""
        with self.assertRaisesRegex(Exception, "already exists"):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "apply_patch",
                    "payload": {"patch": create_existing},
                },
                idempotency_key="patch-create-existing",
            )
        (self.repository / "destination.txt").write_text(
            "destination\n", encoding="utf-8"
        )
        rename_existing = """--- a/sample.txt
+++ b/destination.txt
@@ -1,1 +1,1 @@
-before
+after
"""
        with self.assertRaisesRegex(Exception, "destination already exists"):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "apply_patch",
                    "payload": {"patch": rename_existing},
                },
                idempotency_key="patch-rename-existing",
            )
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        self.assertEqual(
            (self.repository / "destination.txt").read_text(encoding="utf-8"),
            "destination\n",
        )

    def test_patch_rename_preserves_mode_and_updates_content(self) -> None:
        source = self.repository / "sample.txt"
        if os.name != "nt":
            source.chmod(0o640)
        bridge = self._bridge("karox.repo.command")
        patch_text = """--- a/sample.txt
+++ b/renamed.txt
@@ -1,1 +1,1 @@
-before
+after
"""
        result = bridge.execute(
            "karox.repo.command",
            {"action": "apply_patch", "payload": {"patch": patch_text}},
            idempotency_key="patch-rename-success",
        )
        destination = self.repository / "renamed.txt"
        self.assertTrue(result["ok"])
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "after\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)

    def test_invalid_preflight_does_not_reserve_idempotency_key(self) -> None:
        bridge = self._bridge("karox.repo.command")
        key = "patch-corrected-after-invalid"
        with self.assertRaisesRegex(Exception, "hunk counts"):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "apply_patch",
                    "payload": {
                        "patch": """--- a/sample.txt
+++ b/sample.txt
@@ -1,2 +1,1 @@
-before
+after
"""
                    },
                },
                idempotency_key=key,
            )
        result = bridge.execute(
            "karox.repo.command",
            {
                "action": "apply_patch",
                "payload": {
                    "patch": """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-before
+after
"""
                },
            },
            idempotency_key=key,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["idempotent_replay"])

    def test_patch_immediate_retry_replays_without_reapplying_context(self) -> None:
        bridge = self._bridge("karox.repo.command")
        arguments = {
            "action": "apply_patch",
            "payload": {
                "patch": """--- a/sample.txt
+++ b/sample.txt
@@ -1,1 +1,1 @@
-before
+after
"""
            },
        }
        first = bridge.execute(
            "karox.repo.command",
            arguments,
            idempotency_key="patch-immediate-replay",
        )
        second = bridge.execute(
            "karox.repo.command",
            arguments,
            idempotency_key="patch-immediate-replay",
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "after\n",
        )

    def test_transaction_rejects_external_change_before_first_write(self) -> None:
        runtime = ExtendedCoreRuntime(
            self.repository,
            CapabilityPolicy(AccessProfile.WORKSPACE_WRITE),
            self.sessions,
        )
        transaction = WorkspaceTransaction(runtime)
        transaction.stage_write("sample.txt", "transaction\n")
        (self.repository / "sample.txt").write_text("external\n", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "repository changed"):
            transaction.commit()
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "external\n",
        )

    def test_late_commit_failure_rolls_back_earlier_write(self) -> None:
        other = self.repository / "other.txt"
        other.write_text("other-before\n", encoding="utf-8")
        runtime = ExtendedCoreRuntime(
            self.repository,
            CapabilityPolicy(AccessProfile.WORKSPACE_WRITE),
            self.sessions,
        )
        transaction = WorkspaceTransaction(runtime)
        transaction.stage_write("sample.txt", "sample-after\n")
        transaction.stage_write("other.txt", "other-after\n")
        original_atomic_write = WorkspaceTransaction._atomic_write
        calls = 0

        def flaky_atomic_write(path: Path, content: bytes, mode: int | None) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second write failure")
            original_atomic_write(path, content, mode)

        with patch.object(
            WorkspaceTransaction,
            "_atomic_write",
            side_effect=flaky_atomic_write,
        ):
            with self.assertRaisesRegex(OSError, "injected second write failure"):
                transaction.commit()
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        self.assertEqual(other.read_text(encoding="utf-8"), "other-before\n")

    def test_batch_supports_write_move_delete_and_mkdir(self) -> None:
        (self.repository / "delete-me.txt").write_text("gone\n", encoding="utf-8")
        bridge = self._bridge("karox.repo.command")
        result = bridge.execute(
            "karox.repo.command",
            {
                "action": "batch",
                "payload": {
                    "operations": [
                        {"op": "mkdir", "path": "nested"},
                        {"op": "write", "path": "nested/value.txt", "content": "value\n"},
                        {"op": "move", "source": "sample.txt", "destination": "renamed.txt"},
                        {"op": "delete", "path": "delete-me.txt"},
                    ]
                },
            },
            idempotency_key="batch-all",
        )
        self.assertTrue(result["ok"])
        self.assertFalse((self.repository / "sample.txt").exists())
        self.assertTrue((self.repository / "renamed.txt").is_file())
        self.assertFalse((self.repository / "delete-me.txt").exists())
        self.assertEqual(
            (self.repository / "nested" / "value.txt").read_text(encoding="utf-8"),
            "value\n",
        )

    def test_batch_operations_can_depend_on_earlier_operations(self) -> None:
        bridge = self._bridge("karox.repo.command")
        result = bridge.execute(
            "karox.repo.command",
            {
                "action": "batch",
                "payload": {
                    "operations": [
                        {"op": "write", "path": "created.txt", "content": "first\n"},
                        {"op": "move", "source": "created.txt", "destination": "nested/moved.txt"},
                        {"op": "write", "path": "nested/moved.txt", "content": "second\n"},
                    ]
                },
            },
            idempotency_key="batch-dependent",
        )
        self.assertTrue(result["ok"])
        self.assertFalse((self.repository / "created.txt").exists())
        self.assertEqual(
            (self.repository / "nested" / "moved.txt").read_text(encoding="utf-8"),
            "second\n",
        )

    def test_batch_preflight_failure_keeps_every_file_unchanged(self) -> None:
        bridge = self._bridge("karox.repo.command")
        with self.assertRaises(FileNotFoundError):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "write", "path": "first.txt", "content": "one\n"},
                            {"op": "move", "source": "missing.txt", "destination": "x.txt"},
                        ]
                    },
                },
                idempotency_key="batch-preflight",
            )
        self.assertFalse((self.repository / "first.txt").exists())
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )

    def test_repo_command_replay_is_rejected_after_later_change(self) -> None:
        bridge = self._bridge("karox.repo.command")
        arguments = {
            "action": "batch",
            "payload": {
                "operations": [
                    {"op": "write", "path": "sample.txt", "content": "after\n"}
                ]
            },
        }
        bridge.execute(
            "karox.repo.command",
            arguments,
            idempotency_key="batch-replay",
        )
        (self.repository / "sample.txt").write_text("later\n", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "no longer matches repository state"):
            bridge.execute(
                "karox.repo.command",
                arguments,
                idempotency_key="batch-replay",
            )

    def test_mkdir_replay_is_rejected_after_directory_removal(self) -> None:
        bridge = self._bridge("karox.repo.command")
        arguments = {
            "action": "batch",
            "payload": {"operations": [{"op": "mkdir", "path": "created-dir"}]},
        }
        bridge.execute(
            "karox.repo.command",
            arguments,
            idempotency_key="mkdir-replay",
        )
        (self.repository / "created-dir").rmdir()
        with self.assertRaisesRegex(Exception, "no longer matches repository state"):
            bridge.execute(
                "karox.repo.command",
                arguments,
                idempotency_key="mkdir-replay",
            )

    def test_repo_command_rejects_unknown_nested_fields_clearly(self) -> None:
        bridge = self._bridge("karox.repo.command")
        with self.assertRaisesRegex(
            Exception,
            r"batch payload contains unsupported fields: surprise",
        ):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [{"op": "mkdir", "path": "nested"}],
                        "surprise": True,
                    },
                },
                idempotency_key="batch-unknown-payload-field",
            )
        with self.assertRaisesRegex(
            Exception,
            r"operations\[0\] contains unsupported fields: surprise",
        ):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "mkdir", "path": "nested", "surprise": True}
                        ]
                    },
                },
                idempotency_key="batch-unknown-operation-field",
            )

    def test_patch_preserves_crlf_and_supports_delete(self) -> None:
        crlf = self.repository / "crlf.txt"
        crlf.write_bytes(b"one\r\ntwo\r\n")
        bridge = self._bridge("karox.repo.command")
        update = """--- a/crlf.txt
+++ b/crlf.txt
@@ -1,2 +1,2 @@
-one
+changed
 two
"""
        updated = bridge.execute(
            "karox.repo.command",
            {"action": "apply_patch", "payload": {"patch": update}},
            idempotency_key="patch-crlf",
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(crlf.read_bytes(), b"changed\r\ntwo\r\n")

        delete = """--- a/sample.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-before
"""
        deleted = bridge.execute(
            "karox.repo.command",
            {"action": "apply_patch", "payload": {"patch": delete}},
            idempotency_key="patch-delete",
        )
        self.assertTrue(deleted["ok"])
        self.assertFalse((self.repository / "sample.txt").exists())

    def test_batch_is_confined_and_rejects_symlink_escape(self) -> None:
        bridge = self._bridge("karox.repo.command")
        outside = self.root / "outside.txt"
        with self.assertRaises(Exception):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "write", "path": "../outside.txt", "content": "bad\n"}
                        ]
                    },
                },
                idempotency_key="batch-traversal",
            )
        self.assertFalse(outside.exists())

        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        link = self.repository / "escape-link"
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable on this platform")
        with self.assertRaises(Exception):
            bridge.execute(
                "karox.repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {
                                "op": "write",
                                "path": "escape-link/escaped.txt",
                                "content": "bad\n",
                            }
                        ]
                    },
                },
                idempotency_key="batch-symlink-escape",
            )
        self.assertFalse((outside_dir / "escaped.txt").exists())

    def test_structured_tests_run_needs_no_pyproject_rewrite(self) -> None:
        tests = self.repository / "tests"
        tests.mkdir()
        (tests / "test_ok.py").write_text(
            "def test_ok():\n    assert 2 + 2 == 4\n",
            encoding="utf-8",
        )
        bridge = self._bridge("karox.tests.run")
        result = bridge.execute(
            "karox.tests.run",
            {
                "suite": "focused",
                "targets": ["tests/test_ok.py"],
                "timeout_seconds": 60,
            },
            idempotency_key="tests-focused",
            deadline_seconds=90,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["suite"], "focused")
        self.assertEqual(result["data"]["targets"], ["tests/test_ok.py"])
        self.assertEqual(
            result["data"]["argv"][:3],
            [sys.executable, "-m", "pytest"],
        )
        self.assertNotIn("-q", result["data"]["argv"])
        self.assertIn("1 passed", result["data"]["stdout"])

    def test_structured_tests_run_uses_node_package_script_when_repo_has_no_python_tests(self) -> None:
        (self.repository / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}',
            encoding="utf-8",
        )

        class FakeRuntime:
            MAX_PROCESS_TIMEOUT_SECONDS = 600.0

            def __init__(self, repository: Path) -> None:
                self.repository = repository
                self.calls: list[tuple[list[str], float]] = []

            def _run(self, argv: list[str], timeout: float) -> dict[str, object]:
                self.calls.append((list(argv), timeout))
                return {
                    "argv": list(argv),
                    "exit_code": 0,
                    "stdout": "1 passed",
                    "stderr": "",
                    "timed_out": False,
                }

        runtime = FakeRuntime(self.repository)
        result = execute_tests(runtime, {}, 90.0)
        self.assertEqual(result["runner"], "package-script")
        self.assertEqual(result["package_test_script"], "vitest")
        self.assertEqual(runtime.calls[0][0], ["npm", "test", "--", "--run"])
        self.assertEqual(result["exit_code"], 0)

    def test_split_discovery_includes_nested_test_modules(self) -> None:
        nested = self.repository / "tests" / "nested"
        nested.mkdir(parents=True)
        (nested / "test_nested.py").write_text(
            "def test_nested():\n    assert True\n",
            encoding="utf-8",
        )
        self.assertEqual(_test_files(self.repository), ["tests/nested/test_nested.py"])

    def test_runtime_status_exposes_reload_generation(self) -> None:
        bridge = self._bridge("karox.runtime.status")
        data = bridge.execute("karox.runtime.status", {})["data"]
        self.assertTrue(data["hot_reload"])
        self.assertFalse(data["bridge_restart_required"])
        self.assertGreaterEqual(data["generation"], 1)
        self.assertGreaterEqual(data["reload_count"], 0)
        self.assertEqual(len(data["source_sha256"]), 64)
        self.assertEqual(data["generation_sha256"], data["source_sha256"])
        self.assertTrue(data["watched_sources"])
        self.assertTrue(
            any(
                Path(item).name == "workspace_worker.py"
                for item in data["watched_sources"]
            )
        )
        self.assertIn("last_reload_error", data)


class HotWorkerSupervisorTests(unittest.TestCase):
    def test_reload_and_last_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module_path = root / "temporary_worker.py"
            module_path.write_text(
                "def validate_repo_command(*args): return None\n"
                "def execute_repo_command(*args): return {'version': 1}\n"
                "def execute_tests(*args): return {'version': 1}\n"
                "def execute_browser_command(*args): return {'version': 1}\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(root))
            try:
                supervisor = HotWorkerSupervisor()
                supervisor.MODULE_NAME = "temporary_worker"
                first = supervisor.module()
                self.assertEqual(first.execute_repo_command()["version"], 1)

                time.sleep(0.01)
                module_path.write_text(
                    "def validate_repo_command(*args): return None\n"
                    "def execute_repo_command(*args): return {'version': 2}\n"
                    "def execute_tests(*args): return {'version': 2}\n"
                    "def execute_browser_command(*args): return {'version': 2}\n",
                    encoding="utf-8",
                )
                os.utime(module_path, None)
                second = supervisor.module()
                self.assertEqual(second.execute_repo_command()["version"], 2)
                self.assertEqual(supervisor.status()["reload_count"], 1)

                time.sleep(0.01)
                module_path.write_text("def broken(:\n", encoding="utf-8")
                os.utime(module_path, None)
                fallback = supervisor.module()
                self.assertEqual(fallback.execute_repo_command()["version"], 2)
                self.assertIn(
                    "SyntaxError",
                    supervisor.status()["last_reload_error"],
                )
                with self.assertRaisesRegex(RuntimeError, "last known-good"):
                    supervisor.force_reload()
                self.assertEqual(second.execute_repo_command()["version"], 2)
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("temporary_worker", None)

    def test_in_flight_call_finishes_on_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sync_path = root / "temporary_sync.py"
            worker_path = root / "temporary_concurrent_worker.py"
            sync_path.write_text(
                "from threading import Event\nentered = Event()\nrelease = Event()\n",
                encoding="utf-8",
            )

            def source(version: int) -> str:
                return (
                    "from temporary_sync import entered, release\n"
                    f"VERSION = {version}\n"
                    "def validate_repo_command(*args): return None\n"
                    "def execute_repo_command(*args):\n"
                    "    entered.set()\n"
                    "    release.wait(5)\n"
                    "    return {'version': VERSION}\n"
                    "def execute_tests(*args): return {'version': VERSION}\n"
                    "def execute_browser_command(*args): return {'version': VERSION}\n"
                )

            worker_path.write_text(source(1), encoding="utf-8")
            sys.path.insert(0, str(root))
            try:
                supervisor = HotWorkerSupervisor()
                supervisor.MODULE_NAME = "temporary_concurrent_worker"
                supervisor.module()
                sync = __import__("temporary_sync")
                result: dict[str, int] = {}

                def invoke() -> None:
                    result.update(supervisor.execute_repo(None, {}, 1.0))

                thread = threading.Thread(target=invoke)
                thread.start()
                self.assertTrue(sync.entered.wait(2))
                worker_path.write_text(source(2), encoding="utf-8")
                os.utime(worker_path, None)
                second = supervisor.module()
                sync.release.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result["version"], 1)
                self.assertEqual(second.execute_tests()["version"], 2)
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("temporary_sync", None)
                sys.modules.pop("temporary_concurrent_worker", None)

    def test_dependency_change_reloads_the_whole_module_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dependency = root / "temporary_dependency.py"
            worker = root / "temporary_group_worker.py"
            dependency.write_text("VERSION = 1\n", encoding="utf-8")
            worker.write_text(
                "from temporary_dependency import VERSION\n"
                "def validate_repo_command(*args): return None\n"
                "def execute_repo_command(*args): return {'version': VERSION}\n"
                "def execute_tests(*args): return {'version': VERSION}\n"
                "def execute_browser_command(*args): return {'version': VERSION}\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(root))
            try:
                supervisor = HotWorkerSupervisor()
                supervisor.MODULE_NAME = "temporary_group_worker"
                supervisor.MODULE_GROUP = (
                    "temporary_dependency",
                    "temporary_group_worker",
                )
                first = supervisor.module()
                self.assertEqual(first.execute_repo_command()["version"], 1)

                dependency.write_text("VERSION = 2\n", encoding="utf-8")
                os.utime(dependency, None)
                second = supervisor.module()
                self.assertEqual(second.execute_repo_command()["version"], 2)
                self.assertEqual(supervisor.status()["reload_count"], 1)
                self.assertEqual(len(supervisor.status()["watched_sources"]), 2)
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("temporary_dependency", None)
                sys.modules.pop("temporary_group_worker", None)


if __name__ == "__main__":
    unittest.main()
