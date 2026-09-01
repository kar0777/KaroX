from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import initialize_git_repository

from karox.browser_access import BrowserAccessPolicy
from karox.hosted_bridge import HostedBridgeAccessDenied
from karox.hosted_tools_runtime import RUNTIME_RESTART, HostedToolsRuntime
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore
from karox.web_bridge_launcher import WebBridgeConnectConfig


class RuntimeRestartToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.runtime_env = patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": str(self.root / "runtime"),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root / "runtime"),
            },
        )
        self.runtime_env.start()
        self.sessions = SessionStore(self.root / "sessions")
        self.session_id = "runtime-restart-tool"
        self.sessions.create(
            self.repository,
            "Verify safe runtime restart",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id=self.session_id,
        )

    def tearDown(self) -> None:
        self.runtime_env.stop()
        self.temporary.cleanup()

    def _runtime(
        self,
        *,
        saved_profile_name: str | None,
        browser_policy: BrowserAccessPolicy | None = None,
    ) -> HostedToolsRuntime:
        return HostedToolsRuntime(
            self.repository,
            self.sessions,
            self.session_id,
            (RUNTIME_RESTART,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "restart-tool-test"),
            browser_policy=browser_policy,
            saved_profile_name=saved_profile_name,
        )

    def test_saved_workspace_profile_exposes_restart_but_ad_hoc_does_not_auto_add_it(self) -> None:
        saved = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=self.repository,
            saved_profile_name="chatgpt-dev",
            access_profile=AccessProfile.WORKSPACE_WRITE,
            tunnel="custom",
            public_url="https://bridge.example",
        )
        ad_hoc = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=self.repository,
            access_profile=AccessProfile.WORKSPACE_WRITE,
            tunnel="custom",
            public_url="https://bridge.example",
        )
        read_only = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=self.repository,
            saved_profile_name="chatgpt-read",
            access_profile=AccessProfile.READ_ONLY,
            tunnel="custom",
            public_url="https://bridge.example",
        )
        notion = WebBridgeConnectConfig(
            profile="notion",
            repository=self.repository,
            saved_profile_name="notion-write",
            access_profile=AccessProfile.WORKSPACE_WRITE,
            tunnel="custom",
            public_url="https://bridge.example",
        )

        self.assertIn(RUNTIME_RESTART, saved.tools)
        self.assertNotIn(RUNTIME_RESTART, ad_hoc.tools)
        self.assertNotIn(RUNTIME_RESTART, read_only.tools)
        self.assertNotIn(RUNTIME_RESTART, notion.tools)

    def test_restart_descriptor_requires_unique_intent_id(self) -> None:
        runtime = self._runtime(saved_profile_name="chatgpt-dev")
        descriptor = {item.name: item for item in runtime.descriptors()}[RUNTIME_RESTART]
        self.assertEqual(descriptor.input_schema["required"], ["request_id"])
        with self.assertRaises(HostedBridgeAccessDenied):
            runtime.execute(
                RUNTIME_RESTART,
                {"reason": "missing intent id"},
                idempotency_key="restart-tool-idempotency",
            )

    def test_runtime_restart_passes_only_secret_free_saved_identity_and_idempotency(self) -> None:
        runtime = self._runtime(saved_profile_name="chatgpt-dev")
        with patch(
            "karox.runtime_restart.schedule_saved_bridge_child_restart",
            return_value={"ok": True, "status": "restart_scheduled"},
        ) as schedule:
            result = runtime.execute(
                RUNTIME_RESTART,
                {
                    "request_id": "restart-tool-001",
                    "reason": "reload updated guarded runtime",
                },
                idempotency_key="restart-tool-idempotency",
            )

        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["status"], "restart_scheduled")
        schedule.assert_called_once_with(
            session_id=self.session_id,
            saved_profile="chatgpt-dev",
            idempotency_key="restart-tool-idempotency",
            reason="reload updated guarded runtime",
            browser_backend="playwright",
            browser_process_preserved=False,
        )

    def test_direct_runtime_restart_derives_replay_key_from_required_request_id(self) -> None:
        runtime = self._runtime(saved_profile_name="chatgpt-dev")
        with patch(
            "karox.runtime_restart.schedule_saved_bridge_child_restart",
            return_value={"ok": True, "status": "restart_scheduled"},
        ) as schedule:
            result = runtime.execute(
                RUNTIME_RESTART,
                {
                    "request_id": "direct-tool-restart-001",
                    "reason": "recover a degraded hosted bridge",
                },
            )

        self.assertFalse(result.isError)
        schedule.assert_called_once_with(
            session_id=self.session_id,
            saved_profile="chatgpt-dev",
            idempotency_key="runtime-restart:direct-tool-restart-001",
            reason="recover a degraded hosted bridge",
            browser_backend="playwright",
            browser_process_preserved=False,
        )

    def test_restart_refuses_active_playwright_browser_state(self) -> None:
        runtime = self._runtime(saved_profile_name="chatgpt-dev")
        runtime._browser = MagicMock(is_open=True, takeover_active=False)
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "discard the active Playwright browser"):
            runtime.execute(
                RUNTIME_RESTART,
                {"request_id": "restart-playwright-open"},
                idempotency_key="restart-playwright-open-key",
            )

    def test_restart_refuses_any_active_user_takeover(self) -> None:
        policy = BrowserAccessPolicy(
            session_id=self.session_id,
            headed=True,
            user_takeover=True,
            backend="extension",
            saved_profile_id="chatgpt-dev",
        )
        runtime = self._runtime(
            saved_profile_name="chatgpt-dev",
            browser_policy=policy,
        )
        runtime._browser = MagicMock(is_open=True, takeover_active=True)
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "user takeover is active"):
            runtime.execute(
                RUNTIME_RESTART,
                {"request_id": "restart-takeover-active"},
                idempotency_key="restart-takeover-active-key",
            )

    def test_restart_marks_open_managed_extension_browser_as_preserved(self) -> None:
        policy = BrowserAccessPolicy(
            session_id=self.session_id,
            headed=True,
            user_takeover=True,
            backend="extension",
            saved_profile_id="chatgpt-dev",
        )
        runtime = self._runtime(
            saved_profile_name="chatgpt-dev",
            browser_policy=policy,
        )
        runtime._browser = MagicMock(is_open=True, takeover_active=False)
        with patch(
            "karox.runtime_restart.schedule_saved_bridge_child_restart",
            return_value={"ok": True, "status": "restart_scheduled"},
        ) as schedule:
            result = runtime.execute(
                RUNTIME_RESTART,
                {"request_id": "restart-extension-open"},
                idempotency_key="restart-extension-open-key",
            )

        self.assertFalse(result.isError)
        schedule.assert_called_once_with(
            session_id=self.session_id,
            saved_profile="chatgpt-dev",
            idempotency_key="restart-extension-open-key",
            reason="",
            browser_backend="extension",
            browser_process_preserved=True,
        )

    def test_ad_hoc_runtime_cannot_use_restart_even_when_tool_is_explicitly_selected(self) -> None:
        runtime = self._runtime(saved_profile_name=None)
        with self.assertRaises(HostedBridgeAccessDenied):
            runtime.execute(
                RUNTIME_RESTART,
                {},
                idempotency_key="restart-tool-idempotency",
            )


if __name__ == "__main__":
    unittest.main()
