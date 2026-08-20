from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.artifacts import ArtifactStore
from karox.models import AccessProfile
from karox.plan_executor import PlanExecutionError, PlanExecutor
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import TaskStateStore
from test_plan_executor import FakeDelegate


class _FakeGuard:
    def __init__(self, *, changed: bool, supported: bool = True, failed: bool = False) -> None:
        self.supported = supported
        self.changed = changed
        self.failed = failed
        self.changed_paths = ["src/value.txt"] if changed else []
        self.closed = False

    def finish(self) -> bool:
        self.closed = True
        return self.supported and not self.changed and not self.failed

    def close(self) -> None:
        self.closed = True


class PlanExecutorChangeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "value.txt").write_bytes(b"before\n")
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "guard integration",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-guard",
        )
        self.artifacts = ArtifactStore("session-guard")
        self.context = RepositoryContextEngine(
            self.repo,
            self.artifacts,
            policy_profile="workspace_write",
        )
        # Guard-specific integration tests exercise the committed-repository fast
        # path. Unborn repositories intentionally keep the conservative fallback.
        self.context._git("add", ".")
        self.context._git(
            "-c",
            "user.name=KaroX Test",
            "-c",
            "user.email=karox@example.invalid",
            "commit",
            "-m",
            "guard fixture",
        )
        self.delegate = FakeDelegate(self.repo)
        self.executor = PlanExecutor(
            repository=self.repo,
            session_id="session-guard",
            connection_id="chat-guard",
            delegate=self.delegate,
            repo_context=self.context,
            task_states=TaskStateStore(self.sessions),
            artifacts=self.artifacts,
            lease_store=RepositoryLeaseStore(root / "repository-leases"),
            session_directory=self.sessions.session_dir("session-guard"),
        )

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    @staticmethod
    def _plan() -> dict[str, object]:
        return {
            "operations": [
                {
                    "operation_id": "read-value",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                },
                {
                    "operation_id": "search-value",
                    "action": "search",
                    "depends_on": ["read-value"],
                    "inputs": {"query": "before"},
                },
            ]
        }

    def test_quiet_guard_reuses_initial_fast_identity(self) -> None:
        guard = _FakeGuard(changed=False)
        with (
            mock.patch(
                "karox.plan_executor.start_workspace_change_guard",
                return_value=guard,
            ),
            mock.patch.object(
                self.context,
                "_fast_revision_identity",
                wraps=self.context._fast_revision_identity,
            ) as identity,
        ):
            result = self.executor.execute(self._plan(), "guard-quiet")
        self.assertTrue(result["ok"])
        self.assertEqual(identity.call_count, 1)
        self.assertTrue(guard.closed)

    def test_changed_baseline_guard_retries_before_operations(self) -> None:
        guards = [
            _FakeGuard(changed=True),
            _FakeGuard(changed=False),
            _FakeGuard(changed=False),
            _FakeGuard(changed=False),
        ]
        try:
            with (
                mock.patch(
                    "karox.plan_executor.start_workspace_change_guard",
                    side_effect=guards,
                ),
                mock.patch.object(
                    self.context,
                    "_fast_revision_identity",
                    wraps=self.context._fast_revision_identity,
                ) as identity,
            ):
                result = self.executor.execute(self._plan(), "guard-changed-baseline")
            self.assertTrue(result["ok"])
            # The pre-operation event invalidates the first baseline; the second
            # quiet baseline is used for the plan instead of trusting stale state.
            self.assertEqual(identity.call_count, 2)
            self.assertTrue(guards[0].closed)
            self.assertTrue(guards[1].closed)
        finally:
            self.executor._invalidate_read_only_cache()

    @unittest.skipUnless(os.name == "nt", "Windows-only native change guard integration")
    def test_native_guard_detects_same_size_restored_mtime_during_plan(self) -> None:
        target = self.repo / "src" / "value.txt"
        before_stat = target.stat()
        original_execute = self.delegate.execute
        mutated = False
        calls_at_drift = -1

        def execute_with_drift(*args, **kwargs):
            nonlocal mutated, calls_at_drift
            result = original_execute(*args, **kwargs)
            tool_name = args[0] if args else kwargs.get("tool_name")
            if tool_name == "karox.repo.search" and not mutated:
                mutated = True
                calls_at_drift = identity.call_count
                # Same byte length as b"before\\n", then restore the old mtime.
                # The native watcher must treat the write event itself as drift;
                # it must not wait for a second metadata/status identity.
                target.write_bytes(b"after!\n")
                os.utime(
                    target,
                    ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
                )
            return result

        with (
            mock.patch.object(self.delegate, "execute", side_effect=execute_with_drift),
            mock.patch.object(
                self.context,
                "_fast_revision_identity",
                wraps=self.context._fast_revision_identity,
            ) as identity,
        ):
            with self.assertRaises(PlanExecutionError) as caught:
                self.executor.execute(self._plan(), "guard-native-hard-case")
        self.assertEqual(caught.exception.code, "scope_drift")
        self.assertGreaterEqual(calls_at_drift, 1)
        # Once the actual worktree event occurs, finalization trusts the native
        # event and does not try to replace it with another metadata identity.
        self.assertEqual(identity.call_count, calls_at_drift)

    @unittest.skipUnless(os.name == "nt", "Windows-only warm identity cache")
    def test_native_quiet_second_plan_reuses_warm_identity(self) -> None:
        try:
            with mock.patch.object(
                self.context,
                "_fast_revision_identity",
                wraps=self.context._fast_revision_identity,
            ) as identity:
                first = self.executor.execute(self._plan(), "guard-warm-first")
                after_first = identity.call_count
                second = self.executor.execute(self._plan(), "guard-warm-second")
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertGreaterEqual(after_first, 1)
            self.assertEqual(identity.call_count, after_first)
        finally:
            self.executor._invalidate_read_only_cache()

    @unittest.skipUnless(os.name == "nt", "Windows-only warm identity cache")
    def test_native_interplan_change_refreshes_warm_identity(self) -> None:
        target = self.repo / "src" / "value.txt"
        try:
            with mock.patch.object(
                self.context,
                "_fast_revision_identity",
                wraps=self.context._fast_revision_identity,
            ) as identity:
                first = self.executor.execute(self._plan(), "guard-warm-drift-first")
                after_first = identity.call_count
                target.write_bytes(b"changed\n")
                second = self.executor.execute(self._plan(), "guard-warm-drift-second")
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertGreater(identity.call_count, after_first)
        finally:
            self.executor._invalidate_read_only_cache()

    def test_unsupported_guard_preserves_two_identity_fallback(self) -> None:
        guard = _FakeGuard(changed=False, supported=False)
        with (
            mock.patch(
                "karox.plan_executor.start_workspace_change_guard",
                return_value=guard,
            ),
            mock.patch.object(
                self.context,
                "_fast_revision_identity",
                wraps=self.context._fast_revision_identity,
            ) as identity,
        ):
            result = self.executor.execute(self._plan(), "guard-unsupported")
        self.assertTrue(result["ok"])
        self.assertEqual(identity.call_count, 2)
        self.assertTrue(guard.closed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
