from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from typing import Any, Optional

from mcp.types import CallToolResult

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.artifacts import ArtifactStore
from karox.models import AccessProfile
from karox.plan_executor import PlanExecutionError, PlanExecutor
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore


class FakeDelegate:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.calls: list[tuple[str, dict[str, Any], Optional[str]]] = []
        self.fail_once: set[str] = set()
        self.fail_always: set[str] = set()
        self.failed: set[str] = set()
        self.mutate_during: set[str] = set()
        self.access_profile = "workspace_write"
        self.names = {
            "karox.repo.search",
            "karox.repo.read_file",
            "karox.repo.read_lines",
            "karox.runtime.status",
            "karox.runtime.restart",
            "karox.repo.command",
            "karox.tests.run",
            "karox.checks.run",
            "karox.browser.snapshot",
            "karox.dev_server.status",
        }

    def descriptors(self) -> list[Any]:
        return [SimpleNamespace(name=name) for name in sorted(self.names)]

    def session_info(self) -> dict[str, Any]:
        return {"access_profile": self.access_profile}

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
        if tool_name in self.fail_always:
            return {"ok": False, "error_code": "synthetic_failure"}
        if tool_name in self.fail_once and tool_name not in self.failed:
            self.failed.add(tool_name)
            return {"ok": False, "error_code": "synthetic_failure"}
        if tool_name == "karox.repo.search":
            if tool_name in self.mutate_during:
                (self.repository / "unexpected.txt").write_text(
                    "external drift\n", encoding="utf-8"
                )
            return {"ok": True, "match_count": 1, "matches": []}
        if tool_name in {"karox.repo.read_file", "karox.repo.read_lines"}:
            path = self.repository / str(arguments["path"])
            return {"ok": True, "path": str(arguments["path"]), "content": path.read_text(encoding="utf-8")}
        if tool_name == "karox.runtime.status":
            return {"ok": True, "hot_reload": True, "generation": 1}
        if tool_name == "karox.runtime.restart":
            return {
                "ok": True,
                "scheduled": True,
                "request_id": str(arguments.get("request_id") or ""),
            }
        if tool_name == "karox.repo.command":
            payload = arguments.get("payload", {})
            changed: list[str] = []
            for operation in payload.get("operations", []):
                if operation.get("op") != "write":
                    continue
                relative = str(operation["path"])
                path = self.repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(operation["content"]), encoding="utf-8")
                changed.append(relative)
            return {"ok": True, "changed_files": changed}
        if tool_name in {"karox.tests.run", "karox.checks.run", "karox.command.run"}:
            return {"ok": True, "exit_code": 0, "timed_out": False}
        if tool_name == "karox.browser.snapshot":
            return {"ok": True, "title": "fixture"}
        if tool_name == "karox.dev_server.status":
            return {"ok": True, "running": True}
        raise AssertionError(f"unexpected fake tool: {tool_name}")


class PlanExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "value.txt").write_text("before\n", encoding="utf-8")
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Execute bounded plan",
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
        self.delegate = FakeDelegate(self.repo)
        self.executor = PlanExecutor(
            repository=self.repo,
            session_id="session-a",
            connection_id="chat-a",
            delegate=self.delegate,
            repo_context=self.context,
            task_states=TaskStateStore(self.sessions),
            artifacts=self.artifacts,
            lease_store=RepositoryLeaseStore(root / "repository-leases"),
            session_directory=self.sessions.session_dir("session-a"),
        )

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    def test_checkpoint_operations_are_isolated_by_workstream(self) -> None:
        backend_plan = {
            "operations": [
                {
                    "operation_id": "checkpoint-backend",
                    "action": "checkpoint",
                    "inputs": {"updates": {"current_phase": "backend"}},
                }
            ]
        }
        frontend_plan = {
            "operations": [
                {
                    "operation_id": "checkpoint-frontend",
                    "action": "checkpoint",
                    "inputs": {"updates": {"current_phase": "frontend"}},
                }
            ]
        }

        self.executor.execute(
            backend_plan,
            "plan-workstream-backend",
            workstream_id="backend",
        )
        backend_before = self.executor.task_states.load(
            "session-a",
            workstream_id="backend",
        )
        self.executor.execute(
            frontend_plan,
            "plan-workstream-frontend",
            workstream_id="frontend",
        )

        backend_after = self.executor.task_states.load(
            "session-a",
            workstream_id="backend",
        )
        frontend = self.executor.task_states.load(
            "session-a",
            workstream_id="frontend",
        )
        self.assertEqual(backend_after.facts["current_phase"].value, "backend")
        self.assertEqual(frontend.facts["current_phase"].value, "frontend")
        self.assertEqual(backend_after.revision, backend_before.revision)
        self.assertIsNone(self.executor.task_states.load_optional("session-a"))

    @staticmethod
    def _read_plan() -> dict[str, Any]:
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

    @staticmethod
    def _write_plan() -> dict[str, Any]:
        return {
            "operations": [
                {
                    "operation_id": "write-value",
                    "action": "patch",
                    "inputs": {
                        "action": "batch",
                        "payload": {
                            "operations": [
                                {
                                    "op": "write",
                                    "path": "src/value.txt",
                                    "content": "after\n",
                                }
                            ]
                        },
                        "expected_paths": ["src/value.txt"],
                    },
                    "expected_outcome": {"ok": True, "changed_files_min": 1},
                },
                {
                    "operation_id": "verify-value",
                    "action": "checks",
                    "depends_on": ["write-value"],
                    "inputs": {
                        "tool": "tests",
                        "suite": "focused",
                        "targets": ["tests/test_value.py"],
                    },
                    "expected_outcome": {"ok": True, "exit_code": 0},
                },
            ]
        }

    def test_read_only_plan_completes_and_replays_without_second_calls(self) -> None:
        first = self.executor.execute(self._read_plan(), "plan-read")
        call_count = len(self.delegate.calls)
        second = self.executor.execute(self._read_plan(), "plan-read")
        self.assertTrue(first["ok"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len(self.delegate.calls), call_count)
        self.assertTrue(self.artifacts.exists(first["artifact_id"]))

    def test_completed_plan_reports_honest_economy_counters(self) -> None:
        final = self.executor.execute(self._read_plan(), "plan-economy")
        economy = final["economy"]
        # N operations in one hosted call replace N-1 further round trips;
        # the batch planner marks the read-only ones as parallel candidates.
        self.assertEqual(
            economy["model_round_trips_avoided"], final["operations_total"] - 1
        )
        self.assertGreaterEqual(economy["parallel_read_candidates"], 0)
        self.assertGreaterEqual(economy["read_round_trips_saved"], 0)
        self.assertLessEqual(
            economy["read_round_trips_saved"], final["operations_total"]
        )

    def test_elevated_command_operation_routes_through_full_dev_runner(self) -> None:
        self.delegate.access_profile = "elevated"
        self.delegate.names.add("karox.command.run")
        result = self.executor.execute(
            {
                "operations": [
                    {
                        "operation_id": "runtime-status-compatible-read",
                        "action": "read",
                        "inputs": {"mode": "runtime_status"},
                    },
                    {
                        "operation_id": "git-log-compatible-command",
                        "action": "command",
                        "depends_on": ["runtime-status-compatible-read"],
                        "inputs": {"argv": ["git", "log", "-1", "--oneline"]},
                    },
                ]
            },
            "plan-full-command-compatibility",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(
            any(call[0] == "karox.runtime.status" for call in self.delegate.calls)
        )
        self.assertTrue(
            any(call[0] == "karox.command.run" for call in self.delegate.calls)
        )
        self.assertFalse(
            any(call[0] == "karox.checks.run" for call in self.delegate.calls)
        )

    def test_runtime_restart_routes_through_stable_execute_plan_surface(self) -> None:
        result = self.executor.execute(
            {
                "operations": [
                    {
                        "operation_id": "restart-child",
                        "action": "runtime",
                        "inputs": {
                            "verb": "restart",
                            "request_id": "nightly-pass2-smoke",
                            "reason": "verify stable cached tool surface",
                        },
                    }
                ]
            },
            "plan-runtime-restart",
        )
        self.assertTrue(result["ok"])
        restart_calls = [
            call for call in self.delegate.calls if call[0] == "karox.runtime.restart"
        ]
        self.assertEqual(len(restart_calls), 1)
        self.assertEqual(restart_calls[0][1]["request_id"], "nightly-pass2-smoke")
        self.assertIsNotNone(restart_calls[0][2])

    def test_checkpoint_operation_auto_bootstraps_and_accepts_custom_fact(self) -> None:
        result = self.executor.execute(
            {
                "operations": [
                    {
                        "operation_id": "checkpoint-smoke",
                        "action": "checkpoint",
                        "inputs": {"updates": {"smoke": "plan-checkpoint-ok"}},
                    }
                ]
            },
            "plan-checkpoint-auto-bootstrap",
        )
        self.assertTrue(result["ok"])
        state = self.executor.task_states.load("session-a")
        self.assertEqual(state.facts["smoke"].value, "plan-checkpoint-ok")
        self.assertEqual(state.facts["smoke"].origin, FactOrigin.REPORTED_BY_AGENT)
        self.assertEqual(state.facts["repository"].origin, FactOrigin.VERIFIED)

    def test_delegate_exception_blocks_journal_instead_of_leaving_running(self) -> None:
        plan_key = "plan-delegate-exception"
        with mock.patch.object(
            self.delegate,
            "execute",
            side_effect=RuntimeError("synthetic delegate crash"),
        ):
            with self.assertRaises(PlanExecutionError) as caught:
                self.executor.execute(self._read_plan(), plan_key)
        self.assertEqual(caught.exception.code, "delegate_error")
        journal = self.executor.journals.load(plan_key)
        self.assertIsNotNone(journal)
        assert journal is not None
        self.assertEqual(journal["status"], "blocked")
        self.assertEqual(journal["error_code"], "delegate_error")

    def test_read_only_plan_does_not_hash_the_entire_dirty_tree(self) -> None:
        fast_identity = self.executor.repo_context._fast_revision_identity
        with (
            mock.patch.object(
                self.executor.repo_context,
                "_revision_identity",
                side_effect=AssertionError("strict content identity should be lazy"),
            ),
            mock.patch.object(
                self.executor.repo_context,
                "_fast_revision_identity",
                wraps=fast_identity,
            ) as fast_revision,
        ):
            result = self.executor.execute(self._read_plan(), "plan-fast-read")
        self.assertTrue(result["ok"])
        self.assertEqual(result["operations_succeeded"], 2)
        self.assertEqual(fast_revision.call_count, 2)

    def test_pure_read_only_plan_skips_intermediate_journal_fsyncs(self) -> None:
        plan = self._read_plan()
        plan["budgets"] = {"checkpoint_after_steps": 1}
        with mock.patch.object(
            self.executor.journals,
            "mutate",
            wraps=self.executor.journals.mutate,
        ) as mutate:
            result = self.executor.execute(plan, "plan-read-journal-batch")
        self.assertTrue(result["ok"])
        self.assertEqual(mutate.call_count, 1)

    def test_delegate_capabilities_are_snapshotted_once_per_new_plan(self) -> None:
        with mock.patch.object(
            self.delegate,
            "descriptors",
            wraps=self.delegate.descriptors,
        ) as descriptors:
            first = self.executor.execute(self._read_plan(), "plan-capability-snapshot")
            second = self.executor.execute(self._read_plan(), "plan-capability-snapshot")
        self.assertTrue(first["ok"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(descriptors.call_count, 1)

    def test_write_requires_checks_after_final_patch(self) -> None:
        without_checks = {
            "operations": [self._write_plan()["operations"][0]],
        }
        with self.assertRaisesRegex(PlanExecutionError, "after the final write") as caught:
            self.executor.execute(without_checks, "plan-no-checks")
        self.assertEqual(caught.exception.code, "verification_required")
        self.assertEqual(
            (self.repo / "src" / "value.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        checks_first = {
            "operations": [
                self._write_plan()["operations"][1] | {"depends_on": []},
                self._write_plan()["operations"][0],
            ]
        }
        with self.assertRaisesRegex(PlanExecutionError, "after the final write"):
            self.executor.execute(checks_first, "plan-checks-first")

    def test_patch_and_checks_are_journaled_and_not_applied_twice(self) -> None:
        first = self.executor.execute(self._write_plan(), "plan-write")
        patch_calls = [call for call in self.delegate.calls if call[0] == "karox.repo.command"]
        self.assertEqual(len(patch_calls), 1)
        self.assertIsNotNone(patch_calls[0][2])
        self.assertEqual(
            (self.repo / "src" / "value.txt").read_text(encoding="utf-8"),
            "after\n",
        )
        second = self.executor.execute(self._write_plan(), "plan-write")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(
            len([call for call in self.delegate.calls if call[0] == "karox.repo.command"]),
            1,
        )
        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_plan_resumes_after_failure_without_repeating_success(self) -> None:
        plan = {
            "operations": [
                {
                    "operation_id": "read-value",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                },
                {
                    "operation_id": "verify",
                    "action": "checks",
                    "depends_on": ["read-value"],
                    "inputs": {"tool": "tests", "suite": "focused", "targets": ["tests/test_value.py"]},
                },
            ]
        }
        self.delegate.fail_once.add("karox.tests.run")
        with self.assertRaises(PlanExecutionError) as caught:
            self.executor.execute(plan, "plan-resume")
        self.assertEqual(caught.exception.code, "synthetic_failure")
        result = self.executor.execute(plan, "plan-resume")
        self.assertTrue(result["ok"])
        self.assertEqual(
            len([call for call in self.delegate.calls if call[0] == "karox.repo.read_file"]),
            1,
        )
        self.assertEqual(
            len([call for call in self.delegate.calls if call[0] == "karox.tests.run"]),
            2,
        )

    def test_read_operation_cannot_hide_repository_mutation(self) -> None:
        self.delegate.mutate_during.add("karox.repo.search")
        with self.assertRaises(PlanExecutionError) as caught:
            self.executor.execute(
                {
                    "operations": [
                        {
                            "operation_id": "search",
                            "action": "search",
                            "inputs": {"query": "before"},
                        }
                    ]
                },
                "plan-drift",
            )
        self.assertEqual(caught.exception.code, "scope_drift")
        self.assertIn("unexpected.txt", caught.exception.details["observed_paths"])

    def test_patch_must_report_every_observed_path(self) -> None:
        original_execute = self.delegate.execute

        def execute_with_extra(*args: Any, **kwargs: Any) -> dict[str, Any] | CallToolResult:
            result = original_execute(*args, **kwargs)
            if args[0] == "karox.repo.command":
                (self.repo / "extra.txt").write_text("not reported\n", encoding="utf-8")
            return result

        self.delegate.execute = execute_with_extra  # type: ignore[method-assign]
        with self.assertRaises(PlanExecutionError) as caught:
            self.executor.execute(self._write_plan(), "plan-unreported")
        self.assertEqual(caught.exception.code, "scope_drift")
        self.assertIn("extra.txt", caught.exception.details["unexplained_paths"])

    def test_budget_and_idempotency_conflicts_are_rejected(self) -> None:
        plan = self._read_plan()
        too_small = {**plan, "budgets": {"max_steps": 1}}
        with self.assertRaises(PlanExecutionError) as caught:
            self.executor.execute(too_small, "plan-budget")
        self.assertEqual(caught.exception.code, "budget_exceeded")
        self.executor.execute(plan, "plan-conflict")
        changed = self._read_plan()
        changed["operations"][0]["inputs"] = {"path": "src/other.txt"}
        with self.assertRaises(PlanExecutionError) as conflict:
            self.executor.execute(changed, "plan-conflict")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_output_policy_can_force_per_operation_artifact(self) -> None:
        plan = {
            "operations": [
                {
                    "operation_id": "read-artifact",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                    "output_policy": "artifact",
                }
            ]
        }
        result = self.executor.execute(plan, "plan-output-artifact")
        selected = self.artifacts.read_selection(
            result["artifact_id"],
            {
                "kind": "json_path",
                "path": "operations.read-artifact.result",
            },
        )
        operation_result = selected["content"]
        self.assertEqual(operation_result["result_mode"], "artifact")
        self.assertTrue(self.artifacts.exists(operation_result["artifact_id"]))

    def test_repeated_identical_failure_stops_without_endless_retry(self) -> None:
        plan = {
            "operations": [
                {
                    "operation_id": "verify",
                    "action": "checks",
                    "inputs": {
                        "tool": "tests",
                        "suite": "focused",
                        "targets": ["tests/test_value.py"],
                    },
                }
            ],
            "budgets": {"max_fix_attempts": 3},
        }
        self.delegate.fail_always.add("karox.tests.run")
        with self.assertRaises(PlanExecutionError) as first:
            self.executor.execute(plan, "plan-repeat-failure")
        self.assertEqual(first.exception.code, "synthetic_failure")
        with self.assertRaises(PlanExecutionError) as second:
            self.executor.execute(plan, "plan-repeat-failure")
        self.assertEqual(second.exception.code, "repeated_identical_failure")
        self.assertEqual(
            len([call for call in self.delegate.calls if call[0] == "karox.tests.run"]),
            2,
        )

    def test_user_gate_operations_are_refused_inside_plan(self) -> None:
        plan = {
            "operations": [
                {
                    "operation_id": "stop-server",
                    "action": "dev_server",
                    "inputs": {"verb": "stop", "process_id": "owned"},
                }
            ]
        }
        with self.assertRaises(PlanExecutionError) as caught:
            self.executor.execute(plan, "plan-tier2")
        self.assertEqual(caught.exception.code, "session_grant_required")


if __name__ == "__main__":
    unittest.main()
