from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from karox.web_bridge_launcher import (
    TailscaleBackgroundFunnel,
    start_tailscale_background_funnel,
    stop_tailscale_background_funnel,
)


class TailscaleBackgroundFunnelTests(unittest.TestCase):
    def test_start_configures_daemon_background_route(self) -> None:
        plan = SimpleNamespace(
            executable="tailscale",
            public_url="https://monster.example.ts.net",
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="configured", stderr=""
            )
        )
        with (
            patch(
                "karox.web_bridge_launcher.prepare_tailscale_funnel",
                return_value=plan,
            ) as prepare,
            patch(
                "karox.web_bridge_launcher._matching_background_funnel_routes",
                return_value=([object()], []),
            ),
        ):
            tunnel = start_tailscale_background_funnel(
                8765,
                run=run,
            )

        self.assertEqual(tunnel.public_url, plan.public_url)
        self.assertFalse(prepare.call_args.kwargs["restart_service"])
        self.assertEqual(tunnel.port, 8765)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["tailscale", "funnel", "--bg"])
        self.assertIn("--yes", argv)
        self.assertIn("--https=443", argv)
        self.assertIn("http://127.0.0.1:8765", argv)

    def test_start_can_use_parallel_8443_listener(self) -> None:
        plan = SimpleNamespace(
            executable="tailscale",
            public_url="https://monster.example.ts.net:8443",
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="configured", stderr=""
            )
        )
        with (
            patch(
                "karox.web_bridge_launcher.prepare_tailscale_funnel",
                return_value=plan,
            ) as prepare,
            patch(
                "karox.web_bridge_launcher._matching_background_funnel_routes",
                return_value=([object()], []),
            ),
        ):
            tunnel = start_tailscale_background_funnel(
                8767,
                https_port=8443,
                run=run,
                restart_service=False,
            )

        self.assertEqual(tunnel.https_port, 8443)
        self.assertEqual(tunnel.public_url, plan.public_url)
        self.assertIn("--https=8443", run.call_args.args[0])
        self.assertIn("http://127.0.0.1:8767", run.call_args.args[0])
        self.assertEqual(prepare.call_args.kwargs["https_port"], 8443)

    def test_stop_turns_off_only_exact_owned_route(self) -> None:
        tunnel = TailscaleBackgroundFunnel(
            "tailscale", "https://monster.example.ts.net", 8765
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
        )
        owned = object()
        with patch(
            "karox.web_bridge_launcher._matching_background_funnel_routes",
            side_effect=[([owned], []), ([], [])],
        ):
            stopped = stop_tailscale_background_funnel(tunnel, run=run)

        self.assertTrue(stopped)
        self.assertEqual(
            run.call_args.args[0],
            [
                "tailscale",
                "funnel",
                "--https=443",
                "http://127.0.0.1:8765",
                "off",
            ],
        )

    def test_start_hides_the_console_window_on_windows(self) -> None:
        plan = SimpleNamespace(
            executable="tailscale",
            public_url="https://monster.example.ts.net",
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="configured", stderr=""
            )
        )
        with (
            patch("karox.web_bridge_launcher.os.name", "nt"),
            patch(
                "karox.web_bridge_launcher.prepare_tailscale_funnel",
                return_value=plan,
            ),
            patch(
                "karox.web_bridge_launcher._matching_background_funnel_routes",
                return_value=([object()], []),
            ),
        ):
            start_tailscale_background_funnel(8765, run=run)

        # A bridge owner descends from a DETACHED_PROCESS supervisor, so it has no
        # console to inherit. Without this flag Windows allocates a fresh visible
        # console for tailscale.exe and the user sees a window flash on every
        # restart.
        self.assertEqual(
            run.call_args.kwargs.get("creationflags"),
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )

    def test_start_omits_creationflags_off_windows(self) -> None:
        plan = SimpleNamespace(
            executable="tailscale",
            public_url="https://monster.example.ts.net",
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="configured", stderr=""
            )
        )
        with (
            patch("karox.web_bridge_launcher.os.name", "posix"),
            patch(
                "karox.web_bridge_launcher.prepare_tailscale_funnel",
                return_value=plan,
            ),
            patch(
                "karox.web_bridge_launcher._matching_background_funnel_routes",
                return_value=([object()], []),
            ),
        ):
            start_tailscale_background_funnel(8765, run=run)

        self.assertNotIn("creationflags", run.call_args.kwargs)

    def test_stop_hides_the_console_window_on_windows(self) -> None:
        tunnel = TailscaleBackgroundFunnel(
            "tailscale", "https://monster.example.ts.net", 8765
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
        )
        with (
            patch("karox.web_bridge_launcher.os.name", "nt"),
            patch(
                "karox.web_bridge_launcher._matching_background_funnel_routes",
                side_effect=[([object()], []), ([], [])],
            ),
        ):
            self.assertTrue(stop_tailscale_background_funnel(tunnel, run=run))

        self.assertEqual(
            run.call_args.kwargs.get("creationflags"),
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )

    def test_stop_refuses_when_foreign_route_is_present(self) -> None:
        tunnel = TailscaleBackgroundFunnel(
            "tailscale", "https://monster.example.ts.net", 8765
        )
        run = MagicMock()
        with patch(
            "karox.web_bridge_launcher._matching_background_funnel_routes",
            return_value=([object()], [object()]),
        ):
            stopped = stop_tailscale_background_funnel(tunnel, run=run)

        self.assertFalse(stopped)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
