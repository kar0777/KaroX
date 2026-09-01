from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import (
    CHATGPT_PROJECT_BIND,
    CHATGPT_PROJECT_RESUME,
    AutonomyRuntime,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore
from karox.web_bridge_launcher import DEFAULT_WEB_TOOLS


class ChatGPTProjectHostedIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Project-bound ChatGPT work",
            AccessProfile.READ_ONLY,
            branch="main",
            session_id="chatgpt-project-session",
        )
        self.runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "chatgpt-project-session",
            (CHATGPT_PROJECT_BIND, CHATGPT_PROJECT_RESUME),
            access_profile=AccessProfile.READ_ONLY,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "chatgpt-web-project"),
            connection_profile="chatgpt-web",
            client_kind="chatgpt-web",
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    def test_project_tools_ship_in_default_chatgpt_web_bundle(self) -> None:
        self.assertIn(CHATGPT_PROJECT_BIND, DEFAULT_WEB_TOOLS)
        self.assertIn(CHATGPT_PROJECT_RESUME, DEFAULT_WEB_TOOLS)

    def test_read_only_chatgpt_session_can_bind_and_resume_project_context(self) -> None:
        bound = self.runtime.execute(
            CHATGPT_PROJECT_BIND,
            {"project_name": "Ваня не смотри мои чаты"},
        )
        resumed = self.runtime.execute(
            CHATGPT_PROJECT_RESUME,
            {"binding": bound["binding"]},
        )

        self.assertTrue(bound["ok"])
        self.assertTrue(resumed["ok"])
        self.assertEqual(
            resumed["chatgpt_project"]["name"],
            "Ваня не смотри мои чаты",
        )
        self.assertIn("instruction_capsule", bound)
        self.assertIn("Continue from this compact KaroX state", resumed["continuation"]["instruction"])


if __name__ == "__main__":
    unittest.main()
