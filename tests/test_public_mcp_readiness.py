from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from karox.web_bridge_launcher import WebBridgeLaunchError, _wait_for_public_mcp_route


class PublicMcpReadinessTests(unittest.TestCase):
    def test_waits_until_public_auth_layer_is_reachable(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        clock = {"value": 0.0}

        def monotonic() -> float:
            clock["value"] += 0.05
            return clock["value"]

        with (
            patch(
                "karox.web_bridge_launcher.public_mcp_route_healthy",
                side_effect=[False, False, True],
            ) as probe,
            patch("karox.web_bridge_launcher.time.monotonic", side_effect=monotonic),
            patch("karox.web_bridge_launcher.time.sleep"),
        ):
            _wait_for_public_mcp_route(
                process,
                "https://monster.example.ts.net",
                timeout_seconds=2.0,
            )

        self.assertEqual(probe.call_count, 3)
        process.poll.assert_called()

    def test_timeout_refuses_to_announce_public_readiness(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        clock = {"value": 0.0}

        def monotonic() -> float:
            clock["value"] += 0.4
            return clock["value"]

        with (
            patch(
                "karox.web_bridge_launcher.public_mcp_route_healthy",
                return_value=False,
            ),
            patch("karox.web_bridge_launcher.time.monotonic", side_effect=monotonic),
            patch("karox.web_bridge_launcher.time.sleep"),
        ):
            with self.assertRaisesRegex(
                WebBridgeLaunchError,
                "public MCP route reachable",
            ):
                _wait_for_public_mcp_route(
                    process,
                    "https://monster.example.ts.net",
                    timeout_seconds=1.0,
                )

    def test_bridge_exit_aborts_public_readiness_wait(self) -> None:
        process = MagicMock()
        process.poll.return_value = 7

        with self.assertRaisesRegex(WebBridgeLaunchError, "exited with code 7"):
            _wait_for_public_mcp_route(
                process,
                "https://monster.example.ts.net",
                timeout_seconds=1.0,
            )


if __name__ == "__main__":
    unittest.main()
