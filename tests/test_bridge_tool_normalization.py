"""Regression tests for stable worker tools on the direct bridge serve path."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.cli import main
from karox.models import AccessProfile
from karox.sessions import SessionStore


class DirectBridgeToolNormalizationTests(unittest.TestCase):
    def test_direct_serve_keeps_stable_worker_commands(self) -> None:
        """Legacy concrete selections must not hide the stable command surfaces."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            initialize_git_repository(repository)
            sessions_path = root / "sessions"
            SessionStore(sessions_path).create(
                repository,
                "direct bridge tool normalization",
                AccessProfile.WORKSPACE_WRITE,
                session_id="direct-stable-tools",
            )
            captured: dict[str, tuple[str, ...]] = {}

            def core_runtime(
                repository_arg,
                sessions,
                session_id,
                allowed_tool_names,
                **kwargs,
            ):
                del repository_arg, sessions, session_id, kwargs
                captured["core"] = tuple(allowed_tool_names)
                return object()

            def hosted_runtime(
                repository_arg,
                sessions,
                session_id,
                allowed_tool_names,
                **kwargs,
            ):
                del repository_arg, sessions, session_id, kwargs
                captured["extra"] = tuple(allowed_tool_names)
                return object()

            composite = MagicMock()
            composite.descriptors.return_value = []
            credential_store = MagicMock()
            credential_store.resolve.return_value = "test-credential"
            verification = json.dumps(
                [sys.executable, "-m", "pytest", "-q"],
                separators=(",", ":"),
            )
            with (
                patch("karox.cli.session_dir", return_value=sessions_path),
                patch("karox.cli.runtime_dir", return_value=root / "runtime"),
                patch.dict(os.environ, {"KAROX_BROWSER_BACKEND": "playwright"}),
                patch("karox.cli.CoreToolBridge", side_effect=core_runtime),
                patch("karox.cli.HostedToolsRuntime", side_effect=hosted_runtime),
                patch("karox.cli.CompositeHostedBridge", return_value=composite),
                patch("karox.cli.BridgeCredentialStore", return_value=credential_store),
                patch("karox.cli.build_proxy_asgi_app", return_value=object()),
                patch("uvicorn.run") as uvicorn_run,
            ):
                code = main(
                    (
                        "bridge",
                        "serve",
                        "--repository",
                        str(repository),
                        "--session-id",
                        "direct-stable-tools",
                        "--profile",
                        "generic-streamable-http",
                        "--protocol",
                        "mcp",
                        "--tool",
                        "karox.repo.write_file",
                        "--tool",
                        "karox.checks.run",
                        "--tool",
                        "karox.browser.snapshot",
                        "--verification-command",
                        verification,
                        "--credential",
                        "direct-stable-tools",
                    )
                )

        self.assertEqual(code, 0)
        self.assertIn("karox.repo.command", captured["core"])
        self.assertIn("karox.tests.run", captured["core"])
        self.assertIn("karox.browser.command", captured["extra"])
        self.assertEqual(uvicorn_run.call_args.kwargs["timeout_keep_alive"], 300)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
