from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from karox.cli import _upgrade_saved_profile_tools
from karox.models import AccessProfile
from karox.web_bridge_profiles import SavedWebBridgeProfile


class SavedProfileToolUpgradeTests(unittest.TestCase):
    def _profile(self, tools: tuple[str, ...]) -> SavedWebBridgeProfile:
        return SavedWebBridgeProfile(
            name="legacy-write",
            target_profile="chatgpt-web",
            tools=tools,
            access_profile=AccessProfile.WORKSPACE_WRITE,
        )

    def test_legacy_write_profile_gains_plan_executor_without_new_low_level_tools(self) -> None:
        original = (
            "karox.repo.read_file",
            "karox.repo.search",
            "karox.repo.command",
            "karox.tests.run",
        )
        upgraded = _upgrade_saved_profile_tools(self._profile(original))

        self.assertTrue(set(original).issubset(upgraded))
        self.assertIn("karox.task.execute_plan", upgraded)
        self.assertIn("karox.repo.inspect", upgraded)
        self.assertIn("karox.task.status", upgraded)
        self.assertIn("karox.task.resume", upgraded)
        self.assertNotIn("karox.browser.open", upgraded)
        self.assertNotIn("karox.dev_server.start", upgraded)
        self.assertNotIn("karox.checks.run", upgraded)

    def test_profile_without_mutation_and_verification_surface_does_not_gain_executor(self) -> None:
        upgraded = _upgrade_saved_profile_tools(
            self._profile(("karox.repo.read_file", "karox.repo.search"))
        )
        self.assertNotIn("karox.task.execute_plan", upgraded)

    def test_upgrade_is_idempotent(self) -> None:
        profile = self._profile(
            (
                "karox.repo.read_file",
                "karox.repo.command",
                "karox.tests.run",
                "karox.task.execute_plan",
            )
        )
        upgraded = _upgrade_saved_profile_tools(profile)
        self.assertEqual(upgraded.count("karox.task.execute_plan"), 1)

    def test_legacy_chatgpt_profile_gains_durable_autonomy_surface(self) -> None:
        upgraded = _upgrade_saved_profile_tools(
            self._profile(("karox.repo.read_file",))
        )

        for name in (
            "karox.task.workstreams",
            "karox.chatgpt_project.bind",
            "karox.chatgpt_project.resume",
            "karox.intelligence.list",
            "karox.orchestrate.recipes",
            "karox.orchestrate.plan",
            "karox.orchestrate.status",
            "karox.orchestrate.start",
            "karox.orchestrate.control",
            "karox.memory.remember",
            "karox.memory.recall",
        ):
            self.assertIn(name, upgraded)
        self.assertNotIn("karox.browser.open", upgraded)
        self.assertNotIn("karox.repo.write_file", upgraded)


class ProfileIncompatibleAutonomyToolsTests(unittest.TestCase):
    """Autonomy tools are capability-gated like Core and hosted-extra tools."""

    def test_read_only_profile_drops_write_tier_autonomy_tools(self) -> None:
        from karox.web_bridge_launcher import profile_incompatible_tools

        tools = (
            "karox.repo.read_file",
            "karox.task.bootstrap",
            "karox.task.checkpoint",
            "karox.task.execute_plan",
            "karox.checks.run_affected",
            "karox.task.status",
            "karox.task.resume",
            "karox.repo.inspect",
        )
        denied = profile_incompatible_tools(tools, AccessProfile.READ_ONLY)
        self.assertIn("karox.task.checkpoint", denied)
        self.assertIn("karox.task.execute_plan", denied)
        self.assertIn("karox.checks.run_affected", denied)
        for read_tool in (
            "karox.repo.read_file",
            "karox.task.bootstrap",
            "karox.task.status",
            "karox.task.resume",
            "karox.repo.inspect",
        ):
            self.assertNotIn(read_tool, denied)

    def test_workspace_write_profile_keeps_write_tier_autonomy_tools(self) -> None:
        from karox.web_bridge_launcher import profile_incompatible_tools

        tools = (
            "karox.task.checkpoint",
            "karox.task.execute_plan",
            "karox.checks.run_affected",
        )
        self.assertEqual(
            profile_incompatible_tools(tools, AccessProfile.WORKSPACE_WRITE), {}
        )

    def test_elevated_saved_bridge_auto_publishes_local_developer_tools(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path(tmpdir),
                tools=("karox.repo.read_file", "karox.repo.write_file"),
                access_profile=AccessProfile.ELEVATED,
                saved_profile_name="chatgpt-elevated-developer",
            )
        self.assertIn("karox.command.run", config.tools)
        self.assertIn("karox.git.commit", config.tools)
        self.assertIn("karox.chatgpt_project.bind", config.tools)
        self.assertIn("karox.orchestrate.start", config.tools)
        self.assertIn("karox.orchestrate.control", config.tools)

    def test_read_only_saved_chatgpt_bridge_gets_continuity_without_process_control(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path(tmpdir),
                tools=("karox.repo.read_file",),
                access_profile=AccessProfile.READ_ONLY,
                saved_profile_name="chatgpt-readonly-legacy",
            )
        self.assertIn("karox.chatgpt_project.bind", config.tools)
        self.assertIn("karox.chatgpt_project.resume", config.tools)
        self.assertIn("karox.orchestrate.status", config.tools)
        self.assertIn("karox.memory.remember", config.tools)
        self.assertNotIn("karox.orchestrate.start", config.tools)
        self.assertNotIn("karox.orchestrate.control", config.tools)

    def test_ad_hoc_and_non_chatgpt_profiles_keep_exact_autonomy_scope(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            ad_hoc = WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path(tmpdir),
                tools=("karox.repo.read_file",),
                access_profile=AccessProfile.WORKSPACE_WRITE,
            )
            claude = WebBridgeConnectConfig(
                profile="claude-web",
                repository=Path(tmpdir),
                tools=("karox.repo.read_file",),
                access_profile=AccessProfile.WORKSPACE_WRITE,
                saved_profile_name="claude-legacy-minimal",
            )
        for config in (ad_hoc, claude):
            self.assertNotIn("karox.chatgpt_project.bind", config.tools)
            self.assertNotIn("karox.orchestrate.start", config.tools)

    def test_hyperagent_saved_config_keeps_full_only_names_advertised(self) -> None:
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WebBridgeConnectConfig(
                profile="hyperagent-web",
                repository=Path(tmpdir),
                tools=("karox.repo.read_file", "karox.command.run", "karox.git.commit"),
                access_profile=AccessProfile.WORKSPACE_WRITE,
                saved_profile_name="hyperagent-stable-catalog",
            )
        self.assertIn("karox.command.run", config.tools)
        self.assertIn("karox.git.commit", config.tools)
        self.assertIn("karox.command.run", config.profile_denied_tools)
        self.assertIn("karox.git.commit", config.profile_denied_tools)


class AutonomyRuntimeCapabilityGateTests(unittest.TestCase):
    def _runtime(self, tools, access_profile, tmpdir):
        from karox.autonomy_runtime import AutonomyRuntime
        from karox.models import Origin, OriginKind
        from karox.sessions import SessionStore

        sessions = SessionStore(Path(tmpdir))
        repository = Path(tmpdir)
        record = sessions.create(
            task="capability gate test",
            repository=repository,
            access_profile=access_profile,
        )
        return AutonomyRuntime(
            repository=repository,
            sessions=sessions,
            session_id=record.session_id,
            allowed_tool_names=tools,
            access_profile=access_profile,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-autonomy"),
            connection_profile="chatgpt-web",
        )

    def test_read_only_session_fails_fast_on_execute_plan(self) -> None:
        from karox.hosted_bridge import HostedBridgeAccessDenied

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(HostedBridgeAccessDenied):
                self._runtime(
                    ("karox.task.execute_plan",),
                    AccessProfile.READ_ONLY,
                    tmpdir,
                )

    def test_read_only_session_serves_bootstrap_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = self._runtime(
                (
                    "karox.task.bootstrap",
                    "karox.task.status",
                ),
                AccessProfile.READ_ONLY,
                tmpdir,
            )
            names = {item.name for item in runtime.descriptors()}
            self.assertEqual(
                names, {"karox.task.bootstrap", "karox.task.status"}
            )
