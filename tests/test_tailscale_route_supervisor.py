from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from karox.models import AccessProfile
from karox.route_health import RouteHealthTracker
from karox.web_bridge_launcher import (
    TailscaleBackgroundFunnel,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    run_web_bridge,
)


class TailscaleRouteSupervisorTests(unittest.TestCase):
    def _run_case(
        self,
        *,
        route_present: bool,
        local_healthy: bool,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()

            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            credentials.set.return_value = {"secret": "approval-secret"}
            tunnel = TailscaleBackgroundFunnel(
                executable="tailscale",
                public_url="https://monster.example.ts.net",
                port=8765,
            )

            bridge = MagicMock()
            bridge.poll.return_value = None
            bridge.pid = 5101
            bridge.stdout = MagicMock()
            recovered_bridge = MagicMock()
            recovered_bridge.poll.return_value = None
            recovered_bridge.pid = 5102
            recovered_bridge.stdout = MagicMock()

            def terminate_bridge() -> None:
                bridge.poll.return_value = 1

            bridge.terminate.side_effect = terminate_bridge

            mirrored = MagicMock()
            mirrored.detail.return_value = ""
            mirrored.reader = MagicMock()

            sleeps = {"count": 0}

            def sleep(_: float) -> None:
                sleeps["count"] += 1
                if sleeps["count"] >= 3:
                    raise KeyboardInterrupt

            clock = {"value": -1.0}

            def monotonic() -> float:
                clock["value"] += 1.0
                return clock["value"]

            route_value = ([object()], []) if route_present else ([], [])
            route_values = [route_value, route_value, route_value, route_value]
            public_results = [False, False, False, local_healthy, True]

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.RouteHealthTracker",
                    side_effect=lambda: RouteHealthTracker(
                        interval_seconds=0.1,
                        failure_threshold=3,
                    ),
                ),
                patch(
                    "karox.web_bridge_launcher.start_tailscale_background_funnel",
                    return_value=tunnel,
                ) as start_funnel,
                patch(
                    "karox.web_bridge_launcher.refresh_tailscale_background_funnel",
                    return_value=tunnel,
                ) as refresh_funnel,
                patch(
                    "karox.web_bridge_launcher._matching_background_funnel_routes",
                    side_effect=route_values,
                ) as route_inventory,
                patch.object(TailscaleBackgroundFunnel, "stop") as stop_funnel,
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=6101,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen",
                    side_effect=[bridge, recovered_bridge],
                ) as popen,
                patch(
                    "karox.web_bridge_launcher._mirror_child_output",
                    return_value=mirrored,
                ),
                patch("karox.web_bridge_launcher._wait_for_bridge"),
                patch(
                    "karox.web_bridge_launcher._wait_for_public_mcp_route",
                    side_effect=(
                        [None, WebBridgeLaunchError("public route still down")]
                        if not local_healthy
                        else [None]
                    ),
                ),
                patch(
                    "karox.web_bridge_launcher.public_mcp_route_healthy",
                    side_effect=public_results,
                ) as public_probe,
                patch("karox.web_bridge_launcher.time.monotonic", side_effect=monotonic),
                patch("karox.web_bridge_launcher.time.sleep", side_effect=sleep),
            ):
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="chatgpt-web",
                            repository=repository,
                            access_profile=AccessProfile.WORKSPACE_WRITE,
                            tunnel="tailscale",
                            saved_profile_name="hyperagent-auto",
                        )
                    )

        return {
            "code": code,
            "bridge": bridge,
            "recovered_bridge": recovered_bridge,
            "route_inventory": route_inventory,
            "public_probe": public_probe,
            "start_funnel": start_funnel,
            "refresh_funnel": refresh_funnel,
            "stop_funnel": stop_funnel,
            "popen": popen,
            "tunnel": tunnel,
        }

    def test_public_route_and_local_child_failures_recover_independently(self) -> None:
        with self.subTest("public ingress dead but local MCP healthy"):
            result = self._run_case(route_present=True, local_healthy=True)
            self.assertEqual(result["code"], 0)
            result["start_funnel"].assert_called_once()  # type: ignore[union-attr]
            result["refresh_funnel"].assert_called_once_with(  # type: ignore[union-attr]
                result["tunnel"],
                timeout_seconds=30.0,
            )
            self.assertEqual(result["popen"].call_count, 1)  # type: ignore[union-attr]

        with self.subTest("public ingress dead and local MCP child dead"):
            result = self._run_case(route_present=False, local_healthy=False)
            self.assertEqual(result["code"], 0)
            result["start_funnel"].assert_called_once()  # type: ignore[union-attr]
            result["refresh_funnel"].assert_not_called()  # type: ignore[union-attr]
            result["bridge"].terminate.assert_called_once()  # type: ignore[union-attr]
            self.assertEqual(result["popen"].call_count, 2)  # type: ignore[union-attr]
            self.assertGreaterEqual(result["public_probe"].call_count, 4)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
