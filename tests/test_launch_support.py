"""Launcher capability assessment must refuse before anything is spawned.

These tests pin the contract the Connections lifecycle depends on: a saved
connection can be perfectly valid as configuration and still have no managed
launcher, and the reason must come back as a machine-stable code rather than an
opaque failure inside a child process.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connections import McpClientTarget
from karox.launch_support import (
    BLOCKER_AUTH_UNSUPPORTED,
    BLOCKER_CREDENTIAL_MISSING,
    BLOCKER_LAUNCHER_UNAVAILABLE,
    BLOCKER_REPOSITORY_BINDING_MISSING,
    BLOCKER_STABLE_URL_UNAVAILABLE,
    BLOCKER_TUNNEL_UNSUPPORTED,
    ConnectionLaunchResult,
    launch_support,
)


def _target(**overrides: object) -> McpClientTarget:
    """A saved ClickUp connection, which is the one launchable preset today."""

    fields: dict[str, object] = {
        "connection_id": "c-1234567890abcdef",
        "name": "ClickUp",
        "preset_id": "clickup",
        "transport": "streamable_http",
        "endpoint_path": "/mcp",
        "auth_scheme": "bearer",
        "tunnel": "cloudflare",
        "runtime_profile": "clickup",
        "url_stability": "temporary",
        "credential_ref": "os-keyring:connection/c-1234567890abcdef",
    }
    fields.update(overrides)
    return McpClientTarget(**fields)  # type: ignore[arg-type]


class LaunchSupportTests(unittest.TestCase):
    def test_the_saved_clickup_path_is_launchable(self) -> None:
        support = launch_support(_target())
        self.assertTrue(support.supported)
        self.assertEqual(support.blockers, ())
        self.assertEqual(support.launcher_id, "saved-clickup")
        self.assertTrue(support.credential_ready)

    def test_a_preset_without_a_launcher_is_refused_not_silently_started(self) -> None:
        support = launch_support(_target(preset_id="custom"))
        self.assertFalse(support.supported)
        self.assertIn(BLOCKER_LAUNCHER_UNAVAILABLE, support.blockers)
        self.assertIsNone(support.launcher_id)

    def test_repository_bound_custom_bearer_mcp_is_launchable(self) -> None:
        support = launch_support(
            _target(
                preset_id="custom",
                runtime_profile="generic-streamable-http",
                credential_ref="os-keyring:bridge/mcp-c-1234567890abcdef",
            )
        )
        self.assertTrue(support.supported)
        self.assertEqual(support.blockers, ())
        self.assertEqual(support.launcher_id, "saved-mcp")

    def test_legacy_custom_secret_requires_repository_binding_before_launch(self) -> None:
        support = launch_support(
            _target(
                preset_id="custom",
                runtime_profile="generic-streamable-http",
                credential_ref="os-keyring:connection/c-1234567890abcdef",
            )
        )
        self.assertFalse(support.supported)
        self.assertIn(BLOCKER_REPOSITORY_BINDING_MISSING, support.blockers)
        self.assertNotIn(BLOCKER_LAUNCHER_UNAVAILABLE, support.blockers)

    def test_a_quick_tunnel_cannot_be_published_as_a_stable_url(self) -> None:
        # This is the expired-hostname case: promising stability on a Quick
        # Tunnel is what lets a dead URL keep being shown as working.
        support = launch_support(_target(url_stability="stable"))
        self.assertFalse(support.supported)
        self.assertIn(BLOCKER_STABLE_URL_UNAVAILABLE, support.blockers)
        self.assertFalse(support.stable_url)

    def test_tailscale_gives_a_genuinely_stable_url(self) -> None:
        support = launch_support(_target(tunnel="tailscale", url_stability="stable"))
        self.assertTrue(support.supported)
        self.assertTrue(support.stable_url)

    def test_a_user_owned_origin_has_no_managed_launcher_yet(self) -> None:
        # ``custom`` is outside MANAGED_TUNNELS on purpose: KaroX does not own
        # that origin's lifecycle, so it must not claim it can start it.
        support = launch_support(
            _target(tunnel="custom", public_url="https://origin.example")
        )
        self.assertFalse(support.supported)
        self.assertIn(BLOCKER_TUNNEL_UNSUPPORTED, support.blockers)

    def test_an_unresolvable_credential_blocks_the_start(self) -> None:
        support = launch_support(_target(), credential_available=False)
        self.assertFalse(support.supported)
        self.assertIn(BLOCKER_CREDENTIAL_MISSING, support.blockers)
        self.assertFalse(support.credential_ready)

    def test_metadata_only_assessment_trusts_the_saved_reference(self) -> None:
        support = launch_support(_target(), credential_available=None)
        self.assertTrue(support.credential_ready)
        self.assertNotIn(BLOCKER_CREDENTIAL_MISSING, support.blockers)

    def test_a_no_auth_connection_needs_no_credential(self) -> None:
        support = launch_support(
            _target(auth_scheme="none", credential_ref=None),
            credential_available=False,
        )
        self.assertTrue(support.credential_ready)
        self.assertNotIn(BLOCKER_CREDENTIAL_MISSING, support.blockers)

    def test_blockers_are_deduplicated_and_ordered(self) -> None:
        support = launch_support(
            _target(preset_id="custom", tunnel="custom", url_stability="stable"),
            credential_available=False,
        )
        self.assertEqual(len(support.blockers), len(set(support.blockers)))
        self.assertIn(BLOCKER_LAUNCHER_UNAVAILABLE, support.blockers)
        self.assertNotIn(BLOCKER_AUTH_UNSUPPORTED, support.blockers)

    def test_the_assessment_never_carries_a_secret(self) -> None:
        payload = launch_support(_target()).to_dict()
        serialized = repr(payload).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("secret", serialized)


class ConnectionLaunchResultTests(unittest.TestCase):
    def test_a_mapping_result_is_coerced_without_inventing_success(self) -> None:
        result = ConnectionLaunchResult.coerce({"failure_kind": "port_in_use"})
        self.assertFalse(result.success)
        self.assertEqual(result.failure_kind, "port_in_use")
        self.assertIsNone(result.public_endpoint)

    def test_an_object_result_is_coerced(self) -> None:
        class Launched:
            success = True
            public_endpoint = "https://example.test/mcp"
            local_endpoint = "http://127.0.0.1:8765/mcp"

        result = ConnectionLaunchResult.coerce(Launched())
        self.assertTrue(result.success)
        self.assertEqual(result.public_endpoint, "https://example.test/mcp")

    def test_coercing_an_existing_result_is_identity(self) -> None:
        original = ConnectionLaunchResult(success=True)
        self.assertIs(ConnectionLaunchResult.coerce(original), original)

    def test_empty_strings_do_not_become_endpoints(self) -> None:
        result = ConnectionLaunchResult.coerce({"success": True, "public_endpoint": ""})
        self.assertIsNone(result.public_endpoint)
        self.assertEqual(result.to_dict()["public_endpoint"], "")


if __name__ == "__main__":
    unittest.main()
