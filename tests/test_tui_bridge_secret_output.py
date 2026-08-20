from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import tui
from karox.bridge import BridgeCredentialStore


class TuiBridgeSecretOutputTests(unittest.TestCase):
    def test_local_bridge_start_never_prints_raw_secret_and_points_to_copy_command(self) -> None:
        secret = "bridge-secret-that-must-not-reach-scrollback"
        with isolated_karox_directories() as repository:
            app = tui.KaroXApp(repository, language="en")
            app.bridge_process = SimpleNamespace(poll=lambda: None)
            written: list[str] = []
            app._write = lambda text: written.append(str(text))  # type: ignore[method-assign]
            app.call_from_thread = lambda fn, *args, **kwargs: fn(*args, **kwargs)  # type: ignore[method-assign]
            setup = tui.BridgeSetup(
                profile="generic-streamable-http",
                port=8765,
                tools=("karox.runtime.status",),
                tunnel_provider="none",
            )
            launch = tui.BridgeLaunch(
                session_id="bridge-e2e-session",
                profile="generic-streamable-http",
                protocol="mcp",
                endpoint="http://127.0.0.1:8765/mcp",
                secret=secret,
                argv=("python",),
            )
            with mock.patch.object(tui, "_port_is_listening", return_value=True):
                app._confirm_bridge_started(setup, launch)
            app.bridge_launch = launch
            app._refresh_status = lambda: None  # type: ignore[method-assign]
            app._tunnel_ready("https://public.example.com/mcp")

        rendered = "\n".join(written)
        self.assertNotIn(secret, rendered)
        self.assertIn(
            BridgeCredentialStore.fingerprint(secret)[:16],
            rendered,
        )
        self.assertIn(
            "karox bridge credential copy bridge-e2e-session --json",
            rendered,
        )
        self.assertIn("http://127.0.0.1:8765/mcp", rendered)


if __name__ == "__main__":
    unittest.main()
