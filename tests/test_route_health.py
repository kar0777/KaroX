from __future__ import annotations

import unittest
from types import SimpleNamespace

from karox.route_health import RouteHealthTracker, public_mcp_route_healthy


class PublicRouteProbeTests(unittest.TestCase):
    def test_unauthenticated_401_proves_public_ingress_reaches_karox(self) -> None:
        seen: dict[str, object] = {}

        def get(url: str, **kwargs: object) -> SimpleNamespace:
            seen["url"] = url
            seen.update(kwargs)
            return SimpleNamespace(status_code=401)

        self.assertTrue(
            public_mcp_route_healthy(
                "https://monster.example.ts.net/",
                timeout_seconds=1.25,
                get=get,
            )
        )
        self.assertEqual(seen["url"], "https://monster.example.ts.net/mcp")
        self.assertEqual(seen["timeout"], 1.25)
        self.assertIs(seen["follow_redirects"], False)

    def test_custom_mcp_path_is_probed_exactly(self) -> None:
        seen: dict[str, object] = {}

        def get(url: str, **kwargs: object) -> SimpleNamespace:
            seen["url"] = url
            return SimpleNamespace(status_code=401)

        self.assertTrue(
            public_mcp_route_healthy(
                "https://monster.example.ts.net/",
                path="/notion-mcp",
                get=get,
            )
        )
        self.assertEqual(
            seen["url"], "https://monster.example.ts.net/notion-mcp"
        )

    def test_non_401_or_network_failure_is_not_healthy(self) -> None:
        self.assertFalse(
            public_mcp_route_healthy(
                "https://example.ts.net",
                get=lambda *args, **kwargs: SimpleNamespace(status_code=503),
            )
        )

        def broken(*args: object, **kwargs: object) -> object:
            raise OSError("offline")

        self.assertFalse(
            public_mcp_route_healthy("https://example.ts.net", get=broken)
        )


class RouteHealthTrackerTests(unittest.TestCase):
    def test_three_consecutive_failures_request_one_recovery(self) -> None:
        tracker = RouteHealthTracker(interval_seconds=5.0, failure_threshold=3)
        self.assertTrue(tracker.due(0.0))
        self.assertFalse(tracker.observe(now=0.0, healthy=False))
        self.assertFalse(tracker.due(4.99))
        self.assertTrue(tracker.due(5.0))
        self.assertFalse(tracker.observe(now=5.0, healthy=False))
        self.assertTrue(tracker.observe(now=10.0, healthy=False))
        self.assertEqual(tracker.consecutive_failures, 3)

        tracker.recovered(now=10.0)
        self.assertEqual(tracker.consecutive_failures, 0)
        self.assertEqual(tracker.recovery_count, 1)
        self.assertFalse(tracker.due(14.99))
        self.assertTrue(tracker.due(15.0))

    def test_success_resets_the_failure_streak(self) -> None:
        tracker = RouteHealthTracker(interval_seconds=1.0, failure_threshold=2)
        self.assertFalse(tracker.observe(now=0.0, healthy=False))
        self.assertFalse(tracker.observe(now=1.0, healthy=True))
        self.assertEqual(tracker.consecutive_failures, 0)
        self.assertFalse(tracker.observe(now=2.0, healthy=False))

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "interval"):
            RouteHealthTracker(interval_seconds=0)
        with self.assertRaisesRegex(ValueError, "threshold"):
            RouteHealthTracker(failure_threshold=0)


if __name__ == "__main__":
    unittest.main()
