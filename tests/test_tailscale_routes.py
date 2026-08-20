"""Tailscale route inventory and ownership classification.

Phase 0.4: distinguish KaroX-owned routes from foreign ones so the launcher
can reuse its own route, clean up only its own stale route, and never touch a
foreign program's route.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from karox.port_ownership import (
    OWNERSHIP_FREE,
    OWNERSHIP_REUSE_SAME,
    OWNERSHIP_STALE_OWNED,
    OWNERSHIP_UNRELATED,
    OwnershipMetadata,
    OwnershipVerdict,
)
from karox.tailscale_routes import (
    ROUTE_FOREIGN,
    ROUTE_FREE,
    ROUTE_REUSE,
    ROUTE_STALE_OWNED,
    TailscaleRoute,
    _parse_serve_json,
    _parse_serve_text,
    classify_route_for_profile,
    inventory_tailscale_routes,
    route_path_for_profile,
)


def _meta(pid=None, port=8765, proven=True) -> OwnershipMetadata:
    return OwnershipMetadata(
        profile="clickup-opus", credential_reference="os-keyring:bridge/x",
        session_id="x", pid=pid, process_start_time_ns=1 if proven else None,
        executable_path=None, repository=None, local_host="127.0.0.1",
        local_port=port, public_url="https://x", tunnel_type="tailscale",
        route_identity=None, config_digest=None, creation_timestamp=1.0,
        watchdog_path="/x.json", pid_proven=proven,
    )


def _ownership(verdict_code, *, pid=12345, port=8765) -> OwnershipVerdict:
    return OwnershipVerdict(
        verdict=verdict_code,
        reason="test",
        metadata=_meta(pid=pid, port=port),
    )


class ParseServeJsonTests(unittest.TestCase):
    def test_web_handlers_form(self) -> None:
        payload = {
            "Web": {
                "host.ts.net:443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:8765"},
                    }
                }
            }
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 8765)
        self.assertEqual(routes[0].protocol, "https")
        self.assertEqual(routes[0].public_port, 443)
        self.assertEqual(routes[0].path, "/")

    def test_non_default_https_listener_port_is_preserved(self) -> None:
        payload = {
            "Web": {
                "host.ts.net:8443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}
                }
            }
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].public_port, 8443)
        self.assertEqual(routes[0].local_port, 8767)

    def test_flat_form(self) -> None:
        payload = {
            "https://host.ts.net:443": {
                "/": "http://127.0.0.1:9999"
            }
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 9999)

    def test_funnel_mode_detected(self) -> None:
        payload = {
            "Web": {"host.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}},
            "AllowFunnel": {"host.ts.net:443": True},
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(routes[0].mode, "funnel")

    def test_malformed_returns_empty(self) -> None:
        self.assertEqual(_parse_serve_json("not a dict"), [])
        self.assertEqual(_parse_serve_json({}), [])

    def test_deduplicates(self) -> None:
        payload = {
            "Web": {"host.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}},
            "https://host.ts.net:443": {"/": "http://127.0.0.1:8765"},
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(len(routes), 1)


class ParseServeTextTests(unittest.TestCase):
    def test_arrow_form(self) -> None:
        text = "https://host.ts.net/ -> http://127.0.0.1:8765"
        routes = _parse_serve_text(text)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 8765)
        self.assertEqual(routes[0].protocol, "https")

    def test_no_config_returns_empty(self) -> None:
        self.assertEqual(_parse_serve_text("No serve config"), [])
        self.assertEqual(_parse_serve_text(""), [])


class InventoryRoutesTests(unittest.TestCase):
    def test_json_preferred(self) -> None:
        def fake_run(argv, **kw):
            if "--json" in argv:
                return _Completed(0, json.dumps({
                    "Web": {"h:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}
                }), "")
            return _Completed(0, "", "")

        routes = inventory_tailscale_routes("tailscale", run=fake_run)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 8765)

    def test_falls_back_to_text(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "--json" in argv:
                return _Completed(0, "not json", "")
            return _Completed(0, "https://h.ts.net/ -> http://127.0.0.1:8765", "")

        routes = inventory_tailscale_routes("tailscale", run=fake_run)
        self.assertEqual(len(routes), 1)

    def test_no_serve_config(self) -> None:
        def fake_run(argv, **kw):
            return _Completed(0, "No serve config", "")
        routes = inventory_tailscale_routes("tailscale", run=fake_run)
        self.assertEqual(routes, [])

    def test_unavailable_returns_empty(self) -> None:
        def fake_run(argv, **kw):
            raise OSError("not found")
        routes = inventory_tailscale_routes("tailscale", run=fake_run)
        self.assertEqual(routes, [])


class ClassifyRouteTests(unittest.TestCase):
    def test_no_routes_is_free(self) -> None:
        verdict = classify_route_for_profile(
            routes=[], profile_name="clickup-opus", bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_FREE, pid=None),
        )
        self.assertEqual(verdict.verdict, ROUTE_FREE)

    def test_owned_route_live_bridge_is_reuse(self) -> None:
        route = TailscaleRoute("h.ts.net", "/", "https", "http://127.0.0.1:8765", "serve", "k")
        verdict = classify_route_for_profile(
            routes=[route], profile_name="clickup-opus", bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_REUSE_SAME, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_REUSE)
        self.assertIsNotNone(verdict.matching_route)

    def test_owned_route_dead_bridge_is_stale(self) -> None:
        route = TailscaleRoute("h.ts.net", "/", "https", "http://127.0.0.1:8765", "serve", "k")
        verdict = classify_route_for_profile(
            routes=[route], profile_name="clickup-opus", bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_STALE_OWNED, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_STALE_OWNED)
        self.assertIsNotNone(verdict.matching_route)

    def test_foreign_route_is_never_touched(self) -> None:
        foreign = TailscaleRoute("h.ts.net", "/", "https", "http://127.0.0.1:3000", "serve", "k")
        verdict = classify_route_for_profile(
            routes=[foreign], profile_name="clickup-opus", bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_FREE, pid=None),
        )
        self.assertEqual(verdict.verdict, ROUTE_FOREIGN)
        self.assertEqual(len(verdict.foreign_routes), 1)
        self.assertIsNone(verdict.matching_route)

    def test_mixed_owned_and_foreign_preserves_foreign(self) -> None:
        owned = TailscaleRoute("h.ts.net", "/", "https", "http://127.0.0.1:8765", "funnel", "k1")
        foreign = TailscaleRoute("h.ts.net", "/app", "https", "http://127.0.0.1:3000", "serve", "k2")
        verdict = classify_route_for_profile(
            routes=[owned, foreign], profile_name="clickup-opus", bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_REUSE_SAME, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_REUSE)
        self.assertEqual(len(verdict.foreign_routes), 1)


class RoutePathTests(unittest.TestCase):
    def test_profile_path_is_deterministic_and_safe(self) -> None:
        self.assertEqual(route_path_for_profile("clickup-opus"), "/karox/clickup-opus/mcp")
        self.assertEqual(route_path_for_profile("a b/c!d"), "/karox/a-b-c-d/mcp")
        self.assertEqual(route_path_for_profile("   "), "/karox/default/mcp")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


if __name__ == "__main__":
    unittest.main()
