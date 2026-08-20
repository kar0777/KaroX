from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401
from karox.client_capabilities import (
    ClientCapabilityStore,
    negotiate_client_capabilities,
)
from karox.sessions import SessionError


class ClientCapabilitiesTests(unittest.TestCase):
    def test_chatgpt_write_profile_negotiates_full_scoped_capability(self) -> None:
        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=(
                "karox.repo.read_file",
                "karox.repo.inspect",
                "karox.repo.command",
                "karox.task.execute_plan",
                "karox.browser.snapshot",
                "karox.browser.screenshot",
                "karox.artifact.read_image",
            ),
            access_profile="workspace_write",
        )
        self.assertTrue(snapshot.read_support)
        self.assertTrue(snapshot.write_support)
        self.assertTrue(snapshot.browser_support)
        self.assertTrue(snapshot.image_artifact_support)
        self.assertEqual(snapshot.effective_capability, "workspace_write")
        self.assertEqual(snapshot.effective_reason, "configured_and_exposed")
        self.assertEqual(snapshot.approval_behavior, "explicit_user_gates")
        self.assertEqual(snapshot.tool_schema_snapshot_version, 3)
        self.assertEqual(
            snapshot.reconnect_behavior,
            "resume_persistent_session_and_revalidate_repository",
        )

    def test_client_policy_write_block_degrades_honestly_to_read_only(self) -> None:
        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=(
                "karox.repo.read_file",
                "karox.repo.command",
            ),
            access_profile="workspace_write",
            client_policy_write_blocked=True,
        )
        self.assertTrue(snapshot.read_support)
        self.assertFalse(snapshot.write_support)
        self.assertEqual(snapshot.effective_capability, "read_only")
        self.assertEqual(snapshot.effective_reason, "client_policy")

    def test_missing_write_tools_is_not_mislabeled_as_broken_connection(self) -> None:
        snapshot = negotiate_client_capabilities(
            client_kind="other-mcp",
            available_tools=("karox.repo.read_file", "karox.git.status"),
            access_profile="workspace_write",
            disabled_tools=(
                {
                    "name": "karox.repo.command",
                    "reason": "not selected by the connection profile",
                },
            ),
        )
        self.assertEqual(snapshot.effective_capability, "read_only")
        self.assertEqual(snapshot.effective_reason, "profile_configuration")
        self.assertTrue(snapshot.read_support)

    def test_snapshot_is_checksum_protected_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session"
            session.mkdir()
            store = ClientCapabilityStore(session)
            snapshot = negotiate_client_capabilities(
                client_kind="chatgpt-web",
                available_tools=("karox.repo.read_file",),
                access_profile="read_only",
            )
            store.save(snapshot)
            loaded = store.load()
            self.assertEqual(loaded.to_dict(), snapshot.to_dict())
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            payload["effective_capability"] = "workspace_write"
            store.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SessionError, "checksum mismatch"):
                store.load()

    def test_practical_output_limit_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "practical output limit"):
            negotiate_client_capabilities(
                client_kind="chatgpt-web",
                available_tools=("karox.repo.read_file",),
                access_profile="read_only",
                practical_output_size_limit=100,
            )

    def test_read_only_default_bundle_negotiates_read_only(self) -> None:
        """The shipped READ_ONLY bundle must not advertise workspace_write.

        ``karox.task.bootstrap`` ships in the default read-only bundle and
        writes only session task state; counting it as a repo write made every
        read-only launch report itself as workspace_write.
        """

        from karox.web_bridge_launcher import DEFAULT_WEB_TOOLS

        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=DEFAULT_WEB_TOOLS,
            access_profile="read_only",
        )
        self.assertFalse(snapshot.write_support)
        self.assertFalse(snapshot.browser_input_support)
        self.assertEqual(snapshot.effective_capability, "read_only")
        self.assertEqual(snapshot.effective_reason, "access_profile")

    def test_git_commit_counts_as_write_support(self) -> None:
        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=("karox.repo.read_file", "karox.git.commit"),
            access_profile="elevated",
        )
        self.assertTrue(snapshot.write_support)
        self.assertEqual(snapshot.effective_capability, "workspace_write")

    def test_browser_input_without_repo_write_negotiates_browser_control(self) -> None:
        snapshot = negotiate_client_capabilities(
            client_kind="chatgpt-web",
            available_tools=(
                "karox.repo.read_file",
                "karox.browser.snapshot",
                "karox.browser.click",
                "karox.browser.new_tab",
                "karox.browser.switch_tab",
            ),
            access_profile="browser_control",
        )
        self.assertFalse(snapshot.write_support)
        self.assertTrue(snapshot.browser_input_support)
        self.assertEqual(snapshot.effective_capability, "browser_control")
        self.assertEqual(snapshot.effective_reason, "configured_and_exposed")


if __name__ == "__main__":
    unittest.main()
