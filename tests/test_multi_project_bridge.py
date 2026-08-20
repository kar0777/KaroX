from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import AutonomyRuntime, TASK_WORKSTREAMS
from karox.core import InvalidPath
from karox.hosted_bridge import CoreToolBridge, HostedBridgeAccessDenied
from karox.models import AccessProfile, Origin, OriginKind
from karox.project_registry import ProjectEntry, ProjectRegistry
from karox.repository_lease import RepositoryLeaseConflict, RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact


class MultiProjectBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo_a = self.root / "project-a"
        self.repo_b = self.root / "project-b"
        initialize_git_repository(self.repo_a)
        initialize_git_repository(self.repo_b)
        (self.repo_a / "marker.txt").write_text("A\n", encoding="utf-8")
        (self.repo_b / "marker.txt").write_text("B\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repo_a,
            "multi-project hosted task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="multi-project",
        )
        self.registry = ProjectRegistry(
            (
                ProjectEntry("project-a", str(self.repo_a), "Project A"),
                ProjectEntry("project-b", str(self.repo_b), "Project B"),
            ),
            "project-a",
        )
        states = TaskStateStore(self.sessions)
        for workstream, project_id, repository in (
            ("alpha", "project-a", self.repo_a),
            ("beta", "project-b", self.repo_b),
        ):
            states.bootstrap(
                "multi-project",
                {
                    "objective": fact(workstream, FactOrigin.VERIFIED, "test"),
                    "project_id": fact(project_id, FactOrigin.VERIFIED, "project.registry"),
                    "repository": fact(str(repository.resolve()), FactOrigin.VERIFIED, "project.registry"),
                },
                workstream_id=workstream,
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bridge(self) -> CoreToolBridge:
        return CoreToolBridge(
            self.repo_a,
            self.sessions,
            "multi-project",
            ("karox.repo.read_file", "karox.repo.list_files"),
            project_registry=self.registry,
        )

    def test_reads_route_to_workstream_project_without_freeform_path_scope(self) -> None:
        bridge = self._bridge()
        alpha = bridge.execute(
            "karox.repo.read_file",
            {"path": "marker.txt", "workstream_id": "alpha"},
        )
        beta = bridge.execute(
            "karox.repo.read_file",
            {"path": "marker.txt", "workstream_id": "beta"},
        )
        self.assertEqual(alpha["data"]["content"].splitlines(), ["A"])
        self.assertEqual(beta["data"]["content"].splitlines(), ["B"])
        schema = {
            item.name: item.input_schema for item in bridge.descriptors()
        }["karox.repo.read_file"]
        self.assertIn("workstream_id", schema["properties"])
        self.assertNotIn("repository", schema["properties"])
        self.assertNotIn("project_id", schema["properties"])

    def test_traversal_cannot_cross_from_one_approved_project_into_the_other(self) -> None:
        bridge = self._bridge()
        with self.assertRaises(InvalidPath):
            bridge.execute(
                "karox.repo.read_file",
                {"path": "../project-b/marker.txt", "workstream_id": "alpha"},
            )
        with self.assertRaises(InvalidPath):
            bridge.execute(
                "karox.repo.read_file",
                {"path": "../project-a/marker.txt", "workstream_id": "beta"},
            )

    def test_unknown_or_uninitialized_workstream_fails_closed(self) -> None:
        bridge = self._bridge()
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "not initialized"):
            bridge.execute(
                "karox.repo.read_file",
                {"path": "marker.txt", "workstream_id": "gamma"},
            )

    def test_readers_in_two_projects_can_run_concurrently(self) -> None:
        bridge = self._bridge()
        barrier = threading.Barrier(3)
        results: dict[str, str] = {}
        errors: list[BaseException] = []

        def read(workstream: str) -> None:
            try:
                barrier.wait(timeout=5)
                result = bridge.execute(
                    "karox.repo.read_file",
                    {"path": "marker.txt", "workstream_id": workstream},
                )
                results[workstream] = result["data"]["content"]
            except BaseException as exc:  # test thread must report failures to main thread
                errors.append(exc)

        threads = [
            threading.Thread(target=read, args=("alpha",)),
            threading.Thread(target=read, args=("beta",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual({key: value.splitlines() for key, value in results.items()}, {"alpha": ["A"], "beta": ["B"]})
        self.assertIsNot(bridge._runtime_for("project-a"), bridge._runtime_for("project-b"))

    def test_repository_leases_conflict_only_inside_the_same_project(self) -> None:
        store = RepositoryLeaseStore(self.root / "repository-leases")
        lease_a, _ = store.acquire(
            self.repo_a,
            session_id="session-a",
            task_id="task-a",
            connection_id="connection-a",
            current_operation="write-a",
        )
        lease_b, _ = store.acquire(
            self.repo_b,
            session_id="session-b",
            task_id="task-b",
            connection_id="connection-b",
            current_operation="write-b",
        )
        try:
            self.assertNotEqual(lease_a.repository_identity, lease_b.repository_identity)
            with self.assertRaises(RepositoryLeaseConflict):
                store.acquire(
                    self.repo_a,
                    session_id="session-c",
                    task_id="task-c",
                    connection_id="connection-c",
                    current_operation="write-c",
                )
        finally:
            store.release(self.repo_a, lease_a)
            store.release(self.repo_b, lease_b)

    def test_coordinator_summary_exposes_project_identity_not_sibling_context(self) -> None:
        runtime = AutonomyRuntime(
            self.repo_a,
            self.sessions,
            "multi-project",
            (TASK_WORKSTREAMS,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "multi-project-summary"),
            connection_profile="chatgpt-dev",
            project_registry=self.registry,
        )
        try:
            result = runtime.execute(TASK_WORKSTREAMS, {})
        finally:
            runtime.close()
        by_id = {item["workstream_id"]: item for item in result["workstreams"]}
        self.assertEqual(by_id["alpha"]["project_id"], "project-a")
        self.assertEqual(by_id["beta"]["project_id"], "project-b")
        self.assertEqual(by_id["beta"]["project_name"], "Project B")
        self.assertNotIn("repository", by_id["beta"])
        self.assertNotIn("facts", by_id["beta"])
        self.assertEqual(result["default_project_id"], "project-a")

    def test_live_registry_loader_changes_default_and_revokes_removed_project(self) -> None:
        live_registry = [self.registry]
        bridge = CoreToolBridge(
            self.repo_a,
            self.sessions,
            "multi-project",
            ("karox.repo.read_file",),
            project_registry=self.registry,
            project_registry_loader=lambda: live_registry[0],
        )
        live_registry[0] = self.registry.with_default("project-b")
        unscoped = bridge.execute("karox.repo.read_file", {"path": "marker.txt"})
        self.assertEqual(unscoped["data"]["content"].splitlines(), ["B"])

        live_registry[0] = self.registry.remove("project-b")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "no longer approved"):
            bridge.execute(
                "karox.repo.read_file",
                {"path": "marker.txt", "workstream_id": "beta"},
            )


if __name__ == "__main__":
    unittest.main()
