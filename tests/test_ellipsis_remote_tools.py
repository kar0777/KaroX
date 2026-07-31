from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _path_setup
from karox.core import InvalidCommand, VerificationRule
from karox.remote_tools import RemoteCoreRuntime


class EllipsisRemoteToolGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="karox-remote-tools-")
        self.repository = Path(self.temporary.name).resolve()
        runtime = object.__new__(RemoteCoreRuntime)
        runtime.repository = self.repository
        runtime._command_rules = (
            VerificationRule.parse(("python", "-m", "unittest", "*")),
            VerificationRule.parse(("npm", "run", "*")),
            VerificationRule.parse(("powershell", "-NoProfile", "-File", "*")),
        )
        self.runtime = runtime

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_approved_shell_free_command_is_allowed(self) -> None:
        self.assertEqual(
            self.runtime._guarded_argv(["python", "-m", "unittest", "-v"]),
            ["python", "-m", "unittest", "-v"],
        )

    def test_git_and_shell_commands_are_blocked_before_allowlist(self) -> None:
        for argv in (
            ["git", "status"],
            ["cmd", "/c", "echo unsafe"],
            ["bash", "-lc", "echo unsafe"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(InvalidCommand):
                    self.runtime._guarded_argv(argv)

    def test_publish_deploy_and_auth_words_are_blocked(self) -> None:
        for argv in (
            ["npm", "run", "deploy"],
            ["npm", "run", "publish"],
            ["npm", "run", "login"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(InvalidCommand):
                    self.runtime._guarded_argv(argv)

    def test_powershell_requires_existing_repository_script(self) -> None:
        with self.assertRaises(InvalidCommand):
            self.runtime._guarded_argv(
                ["powershell", "-NoProfile", "-Command", "Write-Host unsafe"]
            )
        with self.assertRaises(InvalidCommand):
            self.runtime._guarded_argv(
                ["powershell", "-NoProfile", "-File", "missing.ps1"]
            )
        script = self.repository / "verify.ps1"
        script.write_text("Write-Output 'ok'\n", encoding="utf-8")
        self.assertEqual(
            self.runtime._guarded_argv(
                ["powershell", "-NoProfile", "-File", "verify.ps1"]
            ),
            ["powershell", "-NoProfile", "-File", "verify.ps1"],
        )

    def test_browser_is_limited_to_loopback_hosts(self) -> None:
        self.assertEqual(
            self.runtime._local_url("http://127.0.0.1:3000/path"),
            "http://127.0.0.1:3000/path",
        )
        self.assertEqual(
            self.runtime._local_url("https://localhost:8443/"),
            "https://localhost:8443/",
        )
        for url in (
            "https://example.com/",
            "http://user:pass@localhost/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidCommand):
                    self.runtime._local_url(url)


if __name__ == "__main__":
    unittest.main()
