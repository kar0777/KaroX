from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import (
    CHECKS_RUN_AFFECTED,
    TASK_BOOTSTRAP,
    TASK_CHECKPOINT,
    TASK_EXECUTE_PLAN,
    TASK_RESUME,
    TASK_STATUS,
    TASK_WORKSTREAMS,
    AutonomyRuntime,
    _WorkstreamScopedDelegate,
)
from karox.hosted_bridge import HostedBridgeAccessDenied
from karox.models import AccessProfile, Origin, OriginKind
from karox.project_registry import ProjectEntry, ProjectRegistry
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, fact


class AutonomyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Build GPT Web autonomy foundation",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        self.runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_BOOTSTRAP, TASK_CHECKPOINT, TASK_RESUME, TASK_STATUS, TASK_WORKSTREAMS),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-autonomy"),
            connection_profile="clickup-opus",
            verification_commands=(("python", "-m", "pytest"),),
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    def test_close_releases_plan_executor_resources(self) -> None:
        with mock.patch.object(self.runtime.plan_executor, "close") as closer:
            self.runtime.close()
        closer.assert_called_once_with()

    def test_workstream_delegate_scopes_dev_server_calls(self) -> None:
        delegate = mock.Mock()
        delegate.execute.return_value = {"ok": True}
        scoped = _WorkstreamScopedDelegate(delegate, "frontend")
        result = scoped.execute(
            "karox.dev_server.restart",
            {"process_id": "srv-ui"},
            deadline_seconds=15,
        )
        self.assertTrue(result["ok"])
        delegate.execute.assert_called_once_with(
            "karox.dev_server.restart",
            {"process_id": "srv-ui", "workstream_id": "frontend"},
            idempotency_key=None,
            deadline_seconds=15,
        )

    def test_descriptors_are_small_versioned_high_level_surface(self) -> None:
        descriptors = {item.name: item for item in self.runtime.descriptors()}
        self.assertEqual(
            set(descriptors),
            {TASK_BOOTSTRAP, TASK_CHECKPOINT, TASK_RESUME, TASK_STATUS, TASK_WORKSTREAMS},
        )
        self.assertFalse(descriptors[TASK_BOOTSTRAP].read_only)
        self.assertFalse(descriptors[TASK_CHECKPOINT].read_only)
        self.assertTrue(descriptors[TASK_RESUME].read_only)
        self.assertTrue(descriptors[TASK_STATUS].read_only)
        self.assertTrue(descriptors[TASK_WORKSTREAMS].read_only)
        for name in (TASK_BOOTSTRAP, TASK_CHECKPOINT, TASK_RESUME, TASK_STATUS):
            self.assertIn("workstream_id", descriptors[name].input_schema["properties"])

    def test_run_affected_strips_transport_scope_and_passes_workstream_to_engine(self) -> None:
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (CHECKS_RUN_AFFECTED,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-affected-workstream"),
            connection_profile="clickup-opus",
        )
        try:
            descriptor = {item.name: item for item in runtime.descriptors()}[
                CHECKS_RUN_AFFECTED
            ]
            self.assertIn("workstream_id", descriptor.input_schema["properties"])
            with mock.patch.object(
                runtime.affected_checks,
                "run",
                return_value={"ok": True, "workstream_id": "frontend"},
            ) as run:
                result = runtime._run_affected(
                    {"workstream_id": "frontend", "changed_files": []},
                    "affected-workstream-key",
                )
            self.assertTrue(result["ok"])
            run.assert_called_once_with(
                {"changed_files": []},
                "affected-workstream-key",
                workstream_id="frontend",
            )
        finally:
            runtime.close()

    def test_run_affected_auto_bootstrap_keeps_named_lane_ledger_isolated(self) -> None:
        lease = self.sessions.acquire("session-a", "seed-affected-foreign-ledger", ttl_seconds=5)
        try:
            record = self.sessions.load("session-a")
            record.changed_files = ["foreign-affected.txt"]
            record.checks = [{"ok": True, "command": ["foreign-check"]}]
            self.sessions.save(record, record.revision, lease)
        finally:
            self.sessions.release(lease)

        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (CHECKS_RUN_AFFECTED,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-affected-isolation"),
            connection_profile="clickup-opus",
        )
        engine = mock.Mock()
        engine.run.return_value = {"ok": True}
        try:
            with mock.patch.object(runtime, "_affected_engine_for", return_value=engine):
                result = runtime._run_affected(
                    {"workstream_id": "fresh-affected-lane", "changed_files": []},
                    "affected-isolation-key",
                )
            self.assertTrue(result["ok"])
            facts = runtime.task_states.load(
                "session-a", workstream_id="fresh-affected-lane"
            ).compact()["facts"]
            self.assertEqual(facts["files_changed"]["value"], [])
            self.assertEqual(facts["checks_executed"]["value"], [])
        finally:
            runtime.close()

    def test_execute_plan_auto_bootstrap_keeps_named_lane_ledger_isolated(self) -> None:
        lease = self.sessions.acquire("session-a", "seed-plan-foreign-ledger", ttl_seconds=5)
        try:
            record = self.sessions.load("session-a")
            record.changed_files = ["foreign-plan.txt"]
            record.failures = [{"message": "foreign blocker", "resolved": False}]
            self.sessions.save(record, record.revision, lease)
        finally:
            self.sessions.release(lease)

        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_EXECUTE_PLAN,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-plan-isolation"),
            connection_profile="clickup-opus",
        )
        executor = mock.Mock()
        executor.execute.return_value = {"ok": True}
        arguments = {
            "workstream_id": "fresh-plan-lane",
            "operations": [
                {"operation_id": "read", "action": "read", "inputs": {"path": "README.md"}}
            ],
        }
        try:
            with mock.patch.object(runtime, "_plan_executor_for", return_value=executor):
                result = runtime._execute_plan(arguments, None)
            self.assertTrue(result["ok"])
            facts = runtime.task_states.load(
                "session-a", workstream_id="fresh-plan-lane"
            ).compact()["facts"]
            self.assertEqual(facts["files_changed"]["value"], [])
            self.assertEqual(facts["current_blockers"]["value"], [])
        finally:
            runtime.close()

    def test_execute_plan_canonicalizes_echoed_default_alias_for_idempotency(self) -> None:
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_EXECUTE_PLAN,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-plan-default-alias"),
            connection_profile="clickup-opus",
        )
        executor = mock.Mock()
        executor.execute.return_value = {"ok": True}
        base = {
            "operations": [
                {"operation_id": "read", "action": "read", "inputs": {"path": "README.md"}}
            ]
        }
        try:
            with mock.patch.object(runtime, "_plan_executor_for", return_value=executor):
                self.assertTrue(runtime._execute_plan(dict(base), None)["ok"])
                self.assertTrue(
                    runtime._execute_plan({**base, "workstream_id": "default"}, None)["ok"]
                )
            first = executor.execute.call_args_list[0]
            second = executor.execute.call_args_list[1]
            self.assertEqual(first.args[0], second.args[0])
            self.assertEqual(first.args[1], second.args[1])
            self.assertNotIn("workstream_id", first.args[0])
            self.assertEqual(first.kwargs["workstream_id"], None)
            self.assertEqual(second.kwargs["workstream_id"], None)
        finally:
            runtime.close()

    def test_execute_plan_reports_the_invalid_operation_instead_of_transport_invalid_request(self) -> None:
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_EXECUTE_PLAN,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-autonomy-invalid-plan"),
            connection_profile="clickup-opus",
        )
        try:
            descriptor = {item.name: item for item in runtime.descriptors()}[TASK_EXECUTE_PLAN]
            item_schema = descriptor.input_schema["properties"]["operations"]["items"]
            self.assertNotIn("enum", item_schema["properties"]["action"])
            self.assertTrue(item_schema["additionalProperties"])
            result = runtime.execute(
                TASK_EXECUTE_PLAN,
                {
                    "operations": [
                        {
                            "operation_id": "bad-op",
                            "action": "definitely-not-an-action",
                            "inputs": {},
                        }
                    ]
                },
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "invalid_plan")
            self.assertIn("operations[0].action is invalid", result["error"])
        finally:
            runtime.close()

    def test_execute_plan_descriptor_exposes_budget_contract(self) -> None:
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_EXECUTE_PLAN,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-autonomy-budget-schema"),
            connection_profile="clickup-opus",
        )
        try:
            descriptor = {item.name: item for item in runtime.descriptors()}[TASK_EXECUTE_PLAN]
            budgets = descriptor.input_schema["properties"]["budgets"]
            self.assertFalse(budgets["additionalProperties"])
            self.assertEqual(
                set(budgets["properties"]),
                {
                    "max_steps",
                    "max_wall_time",
                    "max_inline_output_bytes",
                    "max_artifact_bytes",
                    "max_write_operations",
                    "max_command_runs",
                    "max_browser_actions",
                    "max_fix_attempts",
                    "checkpoint_after_steps",
                },
            )
            self.assertEqual(budgets["properties"]["max_steps"]["maximum"], 100)
            self.assertEqual(budgets["properties"]["max_wall_time"]["maximum"], 3600)
        finally:
            runtime.close()

    def test_bootstrap_returns_compact_recovery_snapshot(self) -> None:
        result = self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="bootstrap-1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["permissions"]["git_push"], False)
        self.assertEqual(result["permissions"]["publish"], False)
        self.assertEqual(
            result["verification_commands"],
            [["python", "-m", "pytest"]],
        )
        self.assertIn("dirty_summary", result)
        self.assertIn("relevant_architecture", result)
        facts = result["task"]["facts"]
        self.assertEqual(facts["repository"]["origin"], FactOrigin.VERIFIED.value)
        self.assertEqual(facts["branch"]["origin"], FactOrigin.OBSERVED.value)

    def test_bootstrap_replay_does_not_checkpoint_twice(self) -> None:
        first = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "A"},
            idempotency_key="bootstrap-same",
        )
        state_revision = first["task"]["revision"]
        second = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "A"},
            idempotency_key="bootstrap-same",
        )
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["task"]["revision"], state_revision)

    def test_bootstrap_refresh_without_objective_preserves_existing_workstream_intent(self) -> None:
        first = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "frontend", "objective": "Keep this scoped objective"},
            idempotency_key="bootstrap-objective-first",
        )
        second = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "frontend"},
            idempotency_key="bootstrap-objective-refresh",
        )

        self.assertFalse(second["idempotent_replay"])
        self.assertEqual(second["objective"], "Keep this scoped objective")
        self.assertEqual(
            second["task"]["facts"]["objective"]["value"],
            "Keep this scoped objective",
        )
        self.assertEqual(
            second["task"]["task_id"],
            first["task"]["task_id"],
        )

    def test_bootstrap_refresh_preserves_existing_project_binding_after_reconnect_hint(self) -> None:
        other_repo = Path(self.temp.name) / "other-repo"
        initialize_git_repository(other_repo)
        registry = ProjectRegistry(
            (
                ProjectEntry("anchor", str(self.repo), "Anchor"),
                ProjectEntry("other", str(other_repo), "Other"),
            ),
            "anchor",
        )
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_BOOTSTRAP, TASK_STATUS),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-bootstrap-reconnect"),
            connection_profile="chatgpt-web",
            project_registry=registry,
        )
        try:
            first = runtime.execute(
                TASK_BOOTSTRAP,
                {
                    "workstream_id": "frontend-reconnect",
                    "project_id": "anchor",
                    "objective": "Keep the original project binding",
                },
                idempotency_key="bootstrap-reconnect-first",
            )
            recovered = runtime.execute(
                TASK_BOOTSTRAP,
                {
                    "workstream_id": "frontend-reconnect",
                    "project_id": "other",
                },
                idempotency_key="bootstrap-reconnect-refresh",
            )

            self.assertEqual(first["project_id"], "anchor")
            self.assertEqual(recovered["project_id"], "anchor")
            self.assertTrue(recovered["project_binding_recovered"])
            self.assertEqual(
                recovered["binding_recovery"],
                {
                    "reason": "existing_workstream_binding_preserved",
                    "requested_project_id": "other",
                    "bound_project_id": "anchor",
                    "recovery_action": "continued_with_saved_binding",
                },
            )
            self.assertEqual(
                recovered["task"]["facts"]["project_id"]["value"],
                "anchor",
            )
            self.assertEqual(
                recovered["objective"],
                "Keep the original project binding",
            )
        finally:
            runtime.close()

    def test_checkpoint_auto_bootstraps_when_cached_client_lacks_bootstrap_tool(self) -> None:
        result = self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "updates": {
                    "current_phase": {
                        "value": "smoke",
                        "origin": "reported_by_agent",
                    }
                }
            },
            idempotency_key="checkpoint-without-explicit-bootstrap",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["auto_bootstrapped"])
        facts = result["task"]["facts"]
        self.assertEqual(facts["repository"]["origin"], FactOrigin.VERIFIED.value)
        self.assertEqual(facts["current_phase"]["value"], "smoke")

    def test_named_checkpoint_auto_bootstrap_does_not_inherit_session_global_ledger(self) -> None:
        lease = self.sessions.acquire("session-a", "seed-foreign-ledger", ttl_seconds=5)
        try:
            record = self.sessions.load("session-a")
            record.changed_files = ["foreign-agent.txt"]
            record.checks = [{"ok": True, "command": ["foreign-check"]}]
            record.failures = [{"message": "foreign blocker", "resolved": False}]
            record.unfinished_actions = [{"action": "foreign gate", "requires_user": True}]
            self.sessions.save(record, record.revision, lease)
        finally:
            self.sessions.release(lease)

        result = self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "workstream_id": "fresh-parallel-lane",
                "updates": {
                    "current_phase": {
                        "value": "isolated",
                        "origin": "reported_by_agent",
                    }
                },
            },
            idempotency_key="checkpoint-named-isolation",
        )

        self.assertTrue(result["auto_bootstrapped"])
        facts = result["task"]["facts"]
        self.assertEqual(facts["files_changed"]["value"], [])
        self.assertEqual(facts["checks_executed"]["value"], [])
        self.assertEqual(facts["current_blockers"]["value"], [])
        self.assertEqual(facts["pending_user_gates"]["value"], [])
        self.assertEqual(facts["files_changed"]["evidence"], ["workstream.initial"])
        self.assertEqual(facts["current_phase"]["value"], "isolated")

    def test_custom_checkpoint_auto_bootstraps_without_hidden_fact_whitelist(self) -> None:
        result = self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "updates": {
                    "smoke": {
                        "value": "checkpoint-ok",
                        "origin": "reported_by_agent",
                    }
                }
            },
            idempotency_key="checkpoint-custom-auto-bootstrap",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["auto_bootstrapped"])
        self.assertEqual(result["task"]["facts"]["smoke"]["value"], "checkpoint-ok")
        self.assertEqual(
            result["task"]["facts"]["smoke"]["origin"],
            FactOrigin.REPORTED_BY_AGENT.value,
        )
        self.assertEqual(
            result["task"]["facts"]["repository"]["origin"],
            FactOrigin.VERIFIED.value,
        )

    def test_checkpoint_preserves_agent_provenance_and_replays(self) -> None:
        boot = self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="boot-for-checkpoint",
        )
        revision = boot["task"]["revision"]
        arguments = {
            "updates": {
                "completed_phases": {
                    "value": ["A", "B", "C", "D"],
                    "origin": "reported_by_agent",
                    "evidence": ["focused-tests"],
                },
                "next_safe_action": {
                    "value": "start phase E",
                    "origin": "pending",
                },
            },
            "expected_revision": revision,
        }
        first = self.runtime.execute(
            TASK_CHECKPOINT,
            arguments,
            idempotency_key="checkpoint-1",
        )
        second = self.runtime.execute(
            TASK_CHECKPOINT,
            arguments,
            idempotency_key="checkpoint-1",
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        fact = second["task"]["facts"]["completed_phases"]
        self.assertEqual(fact["origin"], "reported_by_agent")
        self.assertEqual(fact["evidence"], ["focused-tests"])

    def test_checkpoint_cannot_self_assert_verified(self) -> None:
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="boot-before-denial",
        )
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "cannot self-assert"):
            self.runtime.execute(
                TASK_CHECKPOINT,
                {
                    "updates": {
                        "current_phase": {
                            "value": "done",
                            "origin": "verified",
                        }
                    }
                },
                idempotency_key="denied-verified",
            )

    def test_resume_detects_repository_revision_drift(self) -> None:
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="boot-before-drift",
        )
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "new.txt"],
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
                "test drift",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        resumed = self.runtime.execute(TASK_RESUME, {})
        self.assertTrue(resumed["freshness"]["current"])
        self.assertTrue(resumed["freshness"]["recovered"])
        self.assertEqual(resumed["freshness"]["mismatches"], [])
        self.assertIn(
            "repository_revision",
            {item["fact"] for item in resumed["freshness"]["previous_mismatches"]},
        )

    def test_resume_detects_dirty_worktree_drift_without_head_change(self) -> None:
        boot = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "dirty-lane", "objective": "Continue safely"},
            idempotency_key="dirty-lane-bootstrap",
        )
        self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "workstream_id": "dirty-lane",
                "expected_revision": boot["task"]["revision"],
                "updates": {
                    "next_safe_action": {
                        "value": "continue the previous mutation immediately",
                        "origin": "pending",
                    }
                },
            },
            idempotency_key="dirty-lane-checkpoint",
        )
        stored_revision = boot["task"]["facts"]["repository_revision"]["value"]
        (self.repo / "untracked-drift.txt").write_text("changed\n", encoding="utf-8")

        resumed = self.runtime.execute(
            TASK_RESUME,
            {"workstream_id": "dirty-lane"},
        )

        self.assertTrue(resumed["freshness"]["current"])
        self.assertTrue(resumed["freshness"]["recovered"])
        self.assertEqual(resumed["freshness"]["mismatches"], [])
        previous = {
            item["fact"] for item in resumed["freshness"]["previous_mismatches"]
        }
        self.assertIn("working_tree_fingerprint", previous)
        self.assertNotIn("repository_revision", previous)
        self.assertEqual(
            resumed["task"]["facts"]["repository_revision"]["value"],
            stored_revision,
        )
        self.assertEqual(
            resumed["next_safe_action"],
            "inspect current diff and architecture before the next mutation",
        )

    def test_execute_plan_identity_survives_transport_key_change(self) -> None:
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-a",
            (TASK_EXECUTE_PLAN,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-autonomy"),
            connection_profile="clickup-opus",
        )
        plan = {
            "operations": [
                {
                    "operation_id": "read-value",
                    "action": "read",
                    "inputs": {"path": "README.md"},
                }
            ]
        }
        with mock.patch.object(
            runtime.plan_executor,
            "execute",
            return_value={"ok": True},
        ) as execute:
            runtime.execute(
                TASK_EXECUTE_PLAN,
                plan,
                idempotency_key="transport-request-a",
            )
            runtime.execute(
                TASK_EXECUTE_PLAN,
                plan,
                idempotency_key="transport-request-b",
            )
            changed_plan = {
                "operations": [
                    {
                        "operation_id": "read-value-again",
                        "action": "read",
                        "inputs": {"path": "README.md"},
                    }
                ]
            }
            runtime.execute(
                TASK_EXECUTE_PLAN,
                changed_plan,
                idempotency_key="transport-request-c",
            )

        first_key = execute.call_args_list[0].args[1]
        second_key = execute.call_args_list[1].args[1]
        third_key = execute.call_args_list[2].args[1]
        self.assertEqual(first_key, second_key)
        self.assertNotEqual(first_key, third_key)
        self.assertTrue(str(first_key).startswith("execute-plan:"))

    def test_status_does_not_mutate_revision(self) -> None:
        boot = self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="boot-before-status",
        )
        status = self.runtime.execute(TASK_STATUS, {})
        self.assertEqual(status["task"]["revision"], boot["task"]["revision"])

    def test_status_self_heals_dirty_worktree_drift(self) -> None:
        boot = self.runtime.execute(
            TASK_BOOTSTRAP,
            {},
            idempotency_key="boot-before-status-drift",
        )
        (self.repo / "status-drift.txt").write_text("changed\n", encoding="utf-8")

        status = self.runtime.execute(TASK_STATUS, {})

        self.assertTrue(status["freshness"]["current"])
        self.assertTrue(status["freshness"]["recovered"])
        self.assertGreater(status["task"]["revision"], boot["task"]["revision"])
        self.assertIn(
            "working_tree_fingerprint",
            {item["fact"] for item in status["freshness"]["previous_mismatches"]},
        )

    def test_named_workstreams_keep_parallel_chats_independent(self) -> None:
        frontend = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "frontend", "objective": "Build the UI"},
            idempotency_key="boot-frontend",
        )
        backend = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "backend", "objective": "Build the API"},
            idempotency_key="boot-backend",
        )
        self.assertNotEqual(frontend["task"]["task_id"], backend["task"]["task_id"])
        self.assertEqual(frontend["workstream_id"], "frontend")
        self.assertEqual(backend["workstream_id"], "backend")
        self.assertEqual(backend["available_workstreams"], ["backend", "frontend"])

        updated = self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "workstream_id": "frontend",
                "expected_revision": frontend["task"]["revision"],
                "updates": {
                    "current_phase": {
                        "value": "implementation",
                        "origin": "reported_by_agent",
                    }
                },
            },
            idempotency_key="checkpoint-frontend",
        )
        backend_status = self.runtime.execute(
            TASK_STATUS,
            {"workstream_id": "backend"},
        )
        frontend_status = self.runtime.execute(
            TASK_STATUS,
            {"workstream_id": "frontend"},
        )
        self.assertEqual(
            updated["task"]["revision"],
            frontend["task"]["revision"] + 1,
        )
        self.assertEqual(
            backend_status["task"]["revision"],
            backend["task"]["revision"],
        )
        self.assertEqual(
            frontend_status["task"]["facts"]["current_phase"]["value"],
            "implementation",
        )
        self.assertNotIn("current_phase", backend_status["task"]["facts"])

    def test_named_workstream_resume_returns_its_scope_and_siblings(self) -> None:
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "one", "objective": "First chat"},
            idempotency_key="boot-one",
        )
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "two", "objective": "Second chat"},
            idempotency_key="boot-two",
        )
        resumed = self.runtime.execute(TASK_RESUME, {"workstream_id": "one"})
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["workstream_id"], "one")
        self.assertEqual(resumed["available_workstreams"], ["one", "two"])
        self.assertEqual(
            resumed["task"]["facts"]["objective"]["value"],
            "First chat",
        )

    def test_resume_surfaces_active_durable_jobs_before_duplicate_work(self) -> None:
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "Continue durable work"},
            idempotency_key="boot-active-job",
        )
        active = {
            "count": 1,
            "total_count": 1,
            "unreadable_count": 0,
            "jobs": [
                {
                    "job_id": "job-aaaaaaaaaaaaaaaaaaaa",
                    "kind": "command",
                    "status": "running",
                    "updated_at": 1.0,
                    "command": "python",
                    "error_code": None,
                    "artifact_id": None,
                }
            ],
            "truncated": False,
        }
        with mock.patch.object(self.runtime, "_active_durable_jobs", return_value=active):
            resumed = self.runtime.execute(TASK_RESUME, {})

        self.assertEqual(resumed["active_jobs"], active)
        self.assertIn("active durable job", resumed["next_safe_action"])

    def test_legacy_default_task_remains_backward_compatible_with_named_workstreams(self) -> None:
        default = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "Legacy chat"},
            idempotency_key="boot-default",
        )
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "parallel", "objective": "Parallel chat"},
            idempotency_key="boot-parallel",
        )
        status = self.runtime.execute(TASK_STATUS, {})
        self.assertEqual(status["workstream_id"], "default")
        self.assertEqual(status["task"]["task_id"], default["task"]["task_id"])
        self.assertEqual(status["available_workstreams"], ["parallel"])

    def test_echoed_default_workstream_id_does_not_create_a_second_default_lane(self) -> None:
        default = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "Legacy chat"},
            idempotency_key="boot-echo-default",
        )
        echoed = self.runtime.execute(
            TASK_STATUS,
            {"workstream_id": "default"},
        )

        self.assertEqual(echoed["workstream_id"], "default")
        self.assertEqual(echoed["task"]["task_id"], default["task"]["task_id"])
        self.assertNotIn("default", echoed["available_workstreams"])
        self.assertEqual(self.runtime.task_states.list_workstreams("session-a"), ())

    def test_workstream_id_refuses_path_escape(self) -> None:
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "workstream_id"):
            self.runtime.execute(
                TASK_BOOTSTRAP,
                {"workstream_id": "../another-session"},
                idempotency_key="bad-workstream",
            )

    def test_workstreams_returns_compact_coordination_view(self) -> None:
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"objective": "Legacy coordinator task"},
            idempotency_key="coord-default",
        )
        frontend = self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "frontend", "objective": "Build settings UI"},
            idempotency_key="coord-frontend",
        )
        self.runtime.execute(
            TASK_CHECKPOINT,
            {
                "workstream_id": "frontend",
                "expected_revision": frontend["task"]["revision"],
                "updates": {
                    "current_phase": {"value": "implementation", "origin": "reported_by_agent"},
                    "current_blockers": {
                        "value": [
                            "waiting for API shape",
                            "blocker-2",
                            "blocker-3",
                            "blocker-4",
                            "blocker-5",
                        ],
                        "origin": "reported_by_agent",
                    },
                    "files_changed": {
                        "value": [f"src/file-{index}.py" for index in range(12)],
                        "origin": "reported_by_agent",
                    },
                    "next_safe_action": {"value": "wire form", "origin": "pending"},
                },
            },
            idempotency_key="coord-frontend-checkpoint",
        )
        self.runtime.execute(
            TASK_BOOTSTRAP,
            {"workstream_id": "backend", "objective": "Build workstream API"},
            idempotency_key="coord-backend",
        )

        view = self.runtime.execute(TASK_WORKSTREAMS, {})
        self.assertTrue(view["ok"])
        self.assertEqual(view["count"], 3)
        self.assertEqual(view["total_count"], 3)
        self.assertEqual(view["limit"], 24)
        self.assertFalse(view["truncated"])
        self.assertEqual(view["omitted_count"], 0)
        self.assertEqual(
            [item["workstream_id"] for item in view["workstreams"]],
            ["default", "backend", "frontend"],
        )
        frontend_summary = next(
            item for item in view["workstreams"] if item["workstream_id"] == "frontend"
        )
        self.assertEqual(frontend_summary["objective"], "Build settings UI")
        self.assertEqual(frontend_summary["current_phase"], "implementation")
        self.assertEqual(
            frontend_summary["current_blockers"],
            ["waiting for API shape", "blocker-2", "blocker-3", "blocker-4"],
        )
        self.assertEqual(frontend_summary["current_blockers_count"], 5)
        self.assertTrue(frontend_summary["current_blockers_truncated"])
        self.assertEqual(
            frontend_summary["files_changed"],
            [f"src/file-{index}.py" for index in range(8)],
        )
        self.assertEqual(frontend_summary["files_changed_count"], 12)
        self.assertTrue(frontend_summary["files_changed_truncated"])
        self.assertEqual(frontend_summary["next_safe_action"], "wire form")
        self.assertNotIn("facts", frontend_summary)

        named_only = self.runtime.execute(
            TASK_WORKSTREAMS,
            {"include_default": False},
        )
        self.assertEqual(named_only["count"], 2)
        self.assertEqual(
            [item["workstream_id"] for item in named_only["workstreams"]],
            ["backend", "frontend"],
        )

    def test_workstreams_hide_legacy_session_snapshot_from_lane_attribution(self) -> None:
        self.runtime.task_states.bootstrap(
            "session-a",
            {
                "objective": fact("Legacy parallel lane", FactOrigin.VERIFIED, "test.fixture"),
                "files_changed": fact(
                    ["foreign-a.py", "foreign-b.py"],
                    FactOrigin.HISTORICAL,
                    "session.changed_files",
                ),
                "current_blockers": fact(
                    ["foreign blocker"],
                    FactOrigin.HISTORICAL,
                    "session.failures",
                ),
            },
            workstream_id="legacy-lane",
        )

        view = self.runtime.execute(
            TASK_WORKSTREAMS,
            {"include_default": False, "limit": 100},
        )
        summary = next(
            item for item in view["workstreams"] if item["workstream_id"] == "legacy-lane"
        )
        self.assertEqual(summary["files_changed"], [])
        self.assertEqual(summary["files_changed_count"], 0)
        self.assertEqual(summary["legacy_session_files_changed_count"], 2)
        self.assertEqual(summary["current_blockers"], [])
        self.assertEqual(summary["current_blockers_count"], 0)
        self.assertEqual(summary["legacy_session_blockers_count"], 1)
        self.assertEqual(
            summary["legacy_session_snapshot_fields"],
            ["current_blockers", "files_changed"],
        )

        # The raw durable task state is retained for audit/recovery; only the
        # coordination summary stops falsely attributing session-global history.
        raw = self.runtime.task_states.load("session-a", workstream_id="legacy-lane")
        self.assertEqual(raw.facts["files_changed"].value, ["foreign-a.py", "foreign-b.py"])
        self.assertEqual(raw.facts["current_blockers"].value, ["foreign blocker"])

    def test_workstreams_bounds_large_sessions_to_recent_lanes(self) -> None:
        for index in range(25):
            self.runtime.task_states.bootstrap(
                "session-a",
                {
                    "objective": fact(
                        f"Lane {index:02d}", FactOrigin.VERIFIED, "test.fixture"
                    )
                },
                workstream_id=f"lane-{index:02d}",
            )

        view = self.runtime.execute(
            TASK_WORKSTREAMS,
            {"include_default": False},
        )
        self.assertEqual(view["count"], 24)
        self.assertEqual(view["total_count"], 25)
        self.assertTrue(view["truncated"])
        self.assertEqual(view["omitted_count"], 1)
        self.assertEqual(
            [item["workstream_id"] for item in view["workstreams"]],
            [f"lane-{index:02d}" for index in range(1, 25)],
        )

        limited = self.runtime.execute(
            TASK_WORKSTREAMS,
            {"include_default": False, "limit": 3},
        )
        self.assertEqual(limited["count"], 3)
        self.assertEqual(limited["omitted_count"], 22)

class AutonomyBridgeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Bridge contract",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-contract",
        )
        self.runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "session-contract",
            (TASK_BOOTSTRAP, TASK_RESUME, TASK_STATUS),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-bridge-contract"),
            connection_profile="chatgpt-web",
            client_kind="chatgpt-web",
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    def test_status_before_bootstrap_self_heals_verified_base_state(self) -> None:
        status = self.runtime.execute(TASK_STATUS, {})
        self.assertTrue(status["ok"])
        self.assertTrue(status["auto_bootstrapped"])
        self.assertEqual(status["task"]["facts"]["repository"]["origin"], "verified")

    def test_resume_before_bootstrap_self_heals_verified_base_state(self) -> None:
        resumed = self.runtime.execute(TASK_RESUME, {})
        self.assertTrue(resumed["ok"])
        self.assertTrue(resumed["auto_bootstrapped"])
        self.assertTrue(resumed["freshness"]["current"])
        self.assertTrue(resumed["freshness"]["recovered"])

    def test_non_git_bootstrap_reports_not_applicable_without_invoking_git(self) -> None:
        (self.repo / "plain.txt").write_text("plain directory\n", encoding="utf-8")
        with mock.patch.object(
            self.runtime,
            "_git",
            side_effect=AssertionError("Git must not run for a non-Git root"),
        ):
            result = self.runtime.execute(
                TASK_BOOTSTRAP,
                {"objective": "Inspect an allowed plain directory"},
                idempotency_key="non-git-bootstrap",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["repository_kind"], "directory")
        self.assertFalse(result["git_applicable"])
        self.assertIsNone(result["branch"])
        self.assertIsNone(result["repository_revision"])
        self.assertIsNone(result["dirty_summary"]["dirty"])
        facts = result["task"]["facts"]
        self.assertEqual(facts["repository_kind"]["value"], "directory")
        self.assertIn("not_applicable.non_git.branch", facts["branch"]["evidence"])

    def test_web_bridge_capability_snapshot_uses_advertised_output_limit(self) -> None:
        diagnostics = '{"client_capabilities":{"practical_output_size_limit":4194304}}'
        with mock.patch.dict(
            os.environ,
            {"KAROX_BRIDGE_DIAGNOSTICS_JSON": diagnostics},
        ):
            runtime = AutonomyRuntime(
                self.repo,
                self.sessions,
                "session-contract",
                (TASK_STATUS,),
                access_profile=AccessProfile.WORKSPACE_WRITE,
                hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-output-limit"),
                connection_profile="chatgpt-web",
                client_kind="chatgpt-web",
            )
        try:
            self.assertEqual(
                runtime.client_capability_snapshot.practical_output_size_limit,
                4 * 1024 * 1024,
            )
        finally:
            runtime.close()

    def test_elevated_bootstrap_preserves_global_no_push_publish_auth_invariants(self) -> None:
        elevated_session = "session-elevated-contract"
        self.sessions.create(
            self.repo,
            "Elevated bridge",
            AccessProfile.ELEVATED,
            branch="main",
            session_id=elevated_session,
        )
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            elevated_session,
            (TASK_BOOTSTRAP,),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-elevated-invariants"),
            connection_profile="chatgpt-web",
        )
        try:
            with mock.patch.object(
                runtime,
                "_git_snapshot",
                return_value={
                    "repository_kind": "git",
                    "git_applicable": True,
                    "branch": "main",
                    "revision": "deadbeef",
                    "working_tree_fingerprint": "fixture-clean-tree",
                    "dirty": False,
                    "dirty_count": 0,
                    "dirty_summary": [],
                    "dirty_truncated": False,
                },
            ):
                result = runtime.execute(
                    TASK_BOOTSTRAP,
                    {},
                    idempotency_key="elevated-bootstrap",
                )
            self.assertFalse(result["permissions"]["git_push"])
            self.assertFalse(result["permissions"]["publish"])
            self.assertFalse(result["permissions"]["auth_commands"])
            self.assertFalse(result["permissions"]["deploy_release"])
            self.assertFalse(result["permissions"]["destructive_git"])
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
