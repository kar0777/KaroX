from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge, HostedBridgeAccessDenied
from karox.models import AccessProfile
from karox.sessions import SessionStore


class CompositeHostedBridgeRoutingCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repository = root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"before\n")
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repository,
            "routing cache test",
            AccessProfile.WORKSPACE_WRITE,
            session_id="routing-cache",
        )
        self.bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "routing-cache",
            ["karox.repo.read_file"],
        )
        self.composite = CompositeHostedBridge([self.bridge])

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    def test_execute_uses_cached_owner_without_second_descriptor_scan(self) -> None:
        with patch.object(
            self.bridge,
            "descriptors",
            side_effect=AssertionError("owner lookup must not rescan descriptors"),
        ):
            result = self.composite.execute(
                "karox.repo.read_file",
                {"path": "sample.txt"},
            )
        self.assertEqual(result["data"]["content"], "before\n")

    def test_cached_owner_does_not_bypass_revocation(self) -> None:
        self.sessions.revoke("routing-cache")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "revoked"):
            self.composite.execute(
                "karox.repo.read_file",
                {"path": "sample.txt"},
            )

    def test_close_releases_runtime_resources(self) -> None:
        with patch.object(self.bridge, "close", create=True) as closer:
            self.composite.close()
        closer.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
