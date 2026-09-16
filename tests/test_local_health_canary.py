from __future__ import annotations

"""A hung local child must be recycled independently of public probe paths.

The durable owner's tunnel supervision probes public ingress; a process that
stays alive while its listener stops serving passes those liveness checks. The
dead-listener canary closes exactly that hole for tunnels without their own
public probe supervision (Cloudflare/custom), and it is exercised here through
the same owner-loop harness the tunnel supervision tests use.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from itertools import cycle
from pathlib import Path
from unittest.mock import MagicMock, patch

from karox.models import AccessProfile
from karox.route_health import RouteHealthTracker
from karox.web_bridge_launcher import (
    TailscaleBackgroundFunnel,
    WebBridgeConnectConfig,
    run_web_bridge,
)


class LocalBridgeCanaryTests(unittest.TestCase):
    def _run_case(
        self,
        *,
        local_results: list,
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
            bridge.pid = 5201
            bridge.stdout = MagicMock()
            recovered_bridge = MagicMock()
            recovered_bridge.poll.return_value = None
            recovered_bridge.pid = 5202
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
                if sleeps["count"] >= 4:
                    raise KeyboardInterrupt

            clock = {"value": -1.0}

            def monotonic() -> float:
                clock["value"] += 1.0
                return clock["value"]

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
                        interval_seconds=20.0,
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
                    return_value=([object()], []),
                ),
                patch.object(TailscaleBackgroundFunnel, "stop"),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=6201,
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
                ),
                patch(
                    "karox.web_bridge_launcher.public_mcp_route_healthy",
                    return_value=True,
                ),
                patch(
                    "karox.web_bridge_launcher.local_mcp_route_healthy",
                    side_effect=local_results,
                ) as local_probe,
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
                            local_health_interval_seconds=0.05,
                        )
                    )

        return {
            "code": code,
            "bridge": bridge,
            "recovered_bridge": recovered_bridge,
            "start_funnel": start_funnel,
            "refresh_funnel": refresh_funnel,
            "popen": popen,
            "local_probe": local_probe,
        }

    def test_a_hung_listener_is_confirmed_twice_and_recycled_once(self) -> None:
        result = self._run_case(local_results=[False, False] + [True] * 40)
        self.assertEqual(result["code"], 0)
        result["bridge"].terminate.assert_called_once()  # type: ignore[union-attr]
        self.assertEqual(result["popen"].call_count, 2)  # type: ignore[union-attr]
        # No funnel mutation happened for a purely local failure.
        result["start_funnel"].assert_called_once()  # type: ignore[union-attr]
        result["refresh_funnel"].assert_not_called()  # type: ignore[union-attr]

    def test_a_single_catchable_probe_failure_never_recycles_the_child(self) -> None:
        # The owner's own shutdown finally-block terminates a live child, so the
        # observable contract here is: one canary miss recycles nothing - the
        # replacement child never appears and the child object is never
        # terminated while the canary path is active.
        result = self._run_case(local_results=[False, True] + [True] * 40)
        self.assertEqual(result["popen"].call_count, 1)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
