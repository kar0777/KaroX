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
from karox.sessions import SessionStore
from karox.task_state import FactOrigin


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
        self.assertFalse(resumed["freshness"]["current"])
        self.assertEqual(
            resumed["freshness"]["mismatches"][0]["fact"],
            "repository_revision",
        )
        self.assertIn("refresh task.bootstrap", resumed["next_safe_action"])

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
                    "current_blockers": {"value": ["waiting for API shape"], "origin": "reported_by_agent"},
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
        self.assertEqual(
            [item["workstream_id"] for item in view["workstreams"]],
            ["default", "backend", "frontend"],
        )
        frontend_summary = next(
            item for item in view["workstreams"] if item["workstream_id"] == "frontend"
        )
        self.assertEqual(frontend_summary["objective"], "Build settings UI")
        self.assertEqual(frontend_summary["current_phase"], "implementation")
        self.assertEqual(frontend_summary["current_blockers"], ["waiting for API shape"])
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

    def test_status_before_bootstrap_reports_not_initialized_instead_of_policy_denial(self) -> None:
        status = self.runtime.execute(TASK_STATUS, {})
        self.assertFalse(status["ok"])
        self.assertEqual(status["error_code"], "task_not_initialized")
        self.assertIn("task.bootstrap", status["next_safe_action"])

    def test_resume_before_bootstrap_reports_not_initialized_instead_of_policy_denial(self) -> None:
        resumed = self.runtime.execute(TASK_RESUME, {})
        self.assertFalse(resumed["ok"])
        self.assertEqual(resumed["error_code"], "task_not_initialized")
        self.assertIn("task.bootstrap", resumed["next_safe_action"])

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
                    "branch": "main",
                    "revision": "deadbeef",
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
