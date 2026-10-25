"""Tailscale route inventory and ownership classification.

Distinguish KaroX-owned routes from foreign ones so the launcher can reuse its
own route, clean up only its own stale route, and never touch a foreign
program's route. No test here launches or mutates Tailscale.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from karox.bridge_tailscale import (
    TailscaleError,
    TailscaleFunnel,
    _matching_background_funnel_routes,
    background_funnel_argv,
    ensure_tailscale_funnel,
    find_owned_funnel,
    stop_tailscale_funnel,
)
from karox.port_ownership import (
    OWNERSHIP_FREE,
    OWNERSHIP_REUSE_SAME,
    OWNERSHIP_STALE_OWNED,
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
        profile="clickup-opus",
        credential_reference="os-keyring:bridge/x",
        session_id="x",
        pid=pid,
        process_start_time_ns=1 if proven else None,
        executable_path=None,
        repository=None,
        local_host="127.0.0.1",
        local_port=port,
        public_url="https://x",
        tunnel_type="tailscale",
        route_identity=None,
        config_digest=None,
        creation_timestamp=1.0,
        watchdog_path="/x.json",
        pid_proven=proven,
    )


def _ownership(verdict_code, *, pid=12345, port=8765) -> OwnershipVerdict:
    return OwnershipVerdict(
        verdict=verdict_code,
        reason="test",
        metadata=_meta(pid=pid, port=port),
    )


def _route(local_port=8765, path="/", public_port=443, host="host.ts.net") -> TailscaleRoute:
    return TailscaleRoute(
        host,
        path,
        "https",
        f"http://127.0.0.1:{local_port}",
        "funnel",
        f"{host}:{public_port}{path}",
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
        payload = {"https://host.ts.net:443": {"/": "http://127.0.0.1:9999"}}
        routes = _parse_serve_json(payload)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 9999)

    def test_funnel_mode_detected(self) -> None:
        payload = {
            "Web": {
                "host.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
                }
            },
            "AllowFunnel": {"host.ts.net:443": True},
        }
        routes = _parse_serve_json(payload)
        self.assertEqual(routes[0].mode, "funnel")

    def test_malformed_returns_empty(self) -> None:
        self.assertEqual(_parse_serve_json("not a dict"), [])
        self.assertEqual(_parse_serve_json({}), [])

    def test_deduplicates(self) -> None:
        payload = {
            "Web": {
                "host.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
                }
            },
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
                return _Completed(
                    0,
                    json.dumps(
                        {
                            "Web": {
                                "h:443": {
                                    "Handlers": {
                                        "/": {"Proxy": "http://127.0.0.1:8765"}
                                    }
                                }
                            }
                        }
                    ),
                    "",
                )
            return _Completed(0, "", "")

        routes = inventory_tailscale_routes("tailscale", run=fake_run)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].local_port, 8765)

    def test_falls_back_to_text(self) -> None:
        def fake_run(argv, **kw):
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
            routes=[],
            profile_name="clickup-opus",
            bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_FREE, pid=None),
        )
        self.assertEqual(verdict.verdict, ROUTE_FREE)

    def test_owned_route_live_bridge_is_reuse(self) -> None:
        route = _route()
        verdict = classify_route_for_profile(
            routes=[route],
            profile_name="clickup-opus",
            bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_REUSE_SAME, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_REUSE)
        self.assertIsNotNone(verdict.matching_route)

    def test_owned_route_dead_bridge_is_stale(self) -> None:
        route = _route()
        verdict = classify_route_for_profile(
            routes=[route],
            profile_name="clickup-opus",
            bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_STALE_OWNED, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_STALE_OWNED)
        self.assertIsNotNone(verdict.matching_route)

    def test_foreign_route_is_never_touched(self) -> None:
        foreign = _route(local_port=3000)
        verdict = classify_route_for_profile(
            routes=[foreign],
            profile_name="clickup-opus",
            bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_FREE, pid=None),
        )
        self.assertEqual(verdict.verdict, ROUTE_FOREIGN)
        self.assertEqual(len(verdict.foreign_routes), 1)
        self.assertIsNone(verdict.matching_route)

    def test_mixed_owned_and_foreign_preserves_foreign(self) -> None:
        owned = _route()
        foreign = _route(local_port=3000, path="/app")
        verdict = classify_route_for_profile(
            routes=[owned, foreign],
            profile_name="clickup-opus",
            bridge_port=8765,
            bridge_ownership=_ownership(OWNERSHIP_REUSE_SAME, pid=12345),
        )
        self.assertEqual(verdict.verdict, ROUTE_REUSE)
        self.assertEqual(len(verdict.foreign_routes), 1)


class RoutePathTests(unittest.TestCase):
    def test_profile_path_is_deterministic_and_safe(self) -> None:
        self.assertEqual(route_path_for_profile("clickup-opus"), "/karox/clickup-opus/mcp")
        self.assertEqual(route_path_for_profile("a b/c!d"), "/karox/a-b-c-d/mcp")
        self.assertEqual(route_path_for_profile("   "), "/karox/default/mcp")


class BackgroundFunnelArgvTests(unittest.TestCase):
    def test_root_argv_targets_the_exact_loopback_port(self) -> None:
        argv = background_funnel_argv("tailscale", 8765)
        self.assertEqual(
            argv,
            [
                "tailscale",
                "funnel",
                "--bg",
                "--yes",
                "--https=443",
                "http://127.0.0.1:8765",
            ],
        )

    def test_path_argv_uses_a_path_mount(self) -> None:
        argv = background_funnel_argv("tailscale", 8765, https_port=8443, mount_path="/karox/x")
        self.assertIn("--https=8443", argv)
        self.assertIn("--set-path=/karox/x", argv)

    def test_invalid_port_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            background_funnel_argv("tailscale", 0)
        with self.assertRaises(ValueError):
            background_funnel_argv("tailscale", 8765, mount_path="no-slash")


class BackgroundFunnelOwnershipTests(unittest.TestCase):
    def test_matching_routes_split_owned_and_foreign(self) -> None:
        routes = [_route(), _route(local_port=3000), _route(local_port=9999, path="/other")]
        with mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes", return_value=routes
        ):
            owned, foreign = _matching_background_funnel_routes(
                "tailscale", public_url="https://host.ts.net", port=8765
            )
        self.assertEqual([route.local_port for route in owned], [8765])
        self.assertEqual([route.local_port for route in foreign], [3000])

    def test_stop_refuses_to_touch_a_foreign_route(self) -> None:
        tunnel = TailscaleFunnel("tailscale", "https://host.ts.net", 8765)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _Completed(0, "", "")

        with mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes",
            return_value=[_route(local_port=3000)],
        ):
            stopped = stop_tailscale_funnel(tunnel, run=fake_run)
        self.assertFalse(stopped)
        self.assertEqual(calls, [])

    def test_stop_does_nothing_when_no_owned_route_exists(self) -> None:
        tunnel = TailscaleFunnel("tailscale", "https://host.ts.net", 8765)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _Completed(0, "", "")

        with mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes", return_value=[]
        ):
            stopped = stop_tailscale_funnel(tunnel, run=fake_run)
        self.assertTrue(stopped)
        self.assertEqual(calls, [])

    def test_stop_removes_only_the_exact_owned_route(self) -> None:
        tunnel = TailscaleFunnel("tailscale", "https://host.ts.net", 8765)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _Completed(0, "", "")

        responses = [[_route()], []]
        with mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes",
            side_effect=responses,
        ):
            stopped = stop_tailscale_funnel(tunnel, run=fake_run)
        self.assertTrue(stopped)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            ["tailscale", "funnel", "--https=443", "http://127.0.0.1:8765", "off"],
        )

    def test_find_owned_funnel_reports_the_stable_url(self) -> None:
        status = {
            "ready": True,
            "executable": "tailscale",
            "public_url": "https://host.ts.net",
        }
        with mock.patch(
            "karox.bridge_tailscale.query_tailscale_status", return_value=status
        ), mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes", return_value=[_route()]
        ):
            tunnel = find_owned_funnel(8765)
        self.assertIsNotNone(tunnel)
        assert tunnel is not None
        self.assertEqual(tunnel.public_url, "https://host.ts.net")
        self.assertEqual(tunnel.port, 8765)

    def test_find_owned_funnel_refuses_a_foreign_exact_mount(self) -> None:
        status = {
            "ready": True,
            "executable": "tailscale",
            "public_url": "https://host.ts.net",
        }
        with mock.patch(
            "karox.bridge_tailscale.query_tailscale_status", return_value=status
        ), mock.patch(
            "karox.bridge_tailscale.inventory_tailscale_routes",
            return_value=[_route(local_port=3000)],
        ):
            with self.assertRaises(TailscaleError):
                find_owned_funnel(8765)

    def test_ensure_reuses_an_owned_route_without_restarting_it(self) -> None:
        existing = TailscaleFunnel("tailscale", "https://host.ts.net", 8765)
        with mock.patch(
            "karox.bridge_tailscale.find_owned_funnel", return_value=existing
        ), mock.patch(
            "karox.bridge_tailscale.start_tailscale_funnel",
            side_effect=AssertionError("must not start"),
        ):
            tunnel = ensure_tailscale_funnel(8765)
        self.assertEqual(tunnel, existing)


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