"""Tests for the universal connections registry and credential store.

Covers: custom MCP client target creation, model provider creation (via the
existing registry), persistence across restart, secret isolation from the JSON
config, secret edit/delete cascade, built-in connections sharing one runtime
path (ClickUp preset uses the generic-streamable-http profile, not a branch),
URL generation, and the schema migration from a bare pre-versioned file.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connections import (
    CONNECTIONS_SCHEMA_VERSION,
    ConnectionConfigurationError,
    ConnectionCredentialReference,
    ConnectionCredentialStore,
    ConnectionError,
    ConnectionRegistry,
    McpClientPreset,
    McpClientTarget,
    MCP_CLIENT_PRESETS,
    auth_headers,
    build_target_from_preset,
    mcp_client_preset,
    mcp_client_presets,
    mask_secret,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            import keyring.errors  # type: ignore[import-not-found]

            raise keyring.errors.PasswordDeleteError(account)
        del self.store[(service, account)]


def _registry() -> tuple[tempfile.TemporaryDirectory, ConnectionRegistry]:
    tmp = tempfile.TemporaryDirectory()
    return tmp, ConnectionRegistry(Path(tmp.name) / "connections.json")


class ConnectionsRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "connections.json"
        self.registry = ConnectionRegistry(self.path)
        self.backend = _FakeBackend()
        self.credentials = ConnectionCredentialStore(backend=self.backend)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _target(self, name: str = "My ClickUp", preset: str = "clickup") -> McpClientTarget:
        info = self.credentials.set("demo", "sek-1234567890")
        return build_target_from_preset(
            preset,
            name=name,
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
        )

    def test_custom_mcp_client_target_is_created_and_round_trips(self) -> None:
        target = self._target()
        self.registry.put(target)
        loaded = self.registry.list()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "My ClickUp")
        self.assertEqual(loaded[0].preset_id, "clickup")

    def test_connection_persists_and_loads_after_restart(self) -> None:
        target = self._target()
        self.registry.put(target)
        # Simulate a restart by dropping the in-memory registry and rebuilding.
        reopened = ConnectionRegistry(self.path)
        loaded = reopened.list()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].connection_id, target.connection_id)

    def test_secret_is_not_persisted_in_plain_config(self) -> None:
        target = self._target()
        self.registry.put(target)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sek-1234567890", raw)
        payload = json.loads(raw)
        self.assertEqual(payload["schema_version"], CONNECTIONS_SCHEMA_VERSION)
        # Only an opaque reference is stored, never the value.
        self.assertTrue(
            target.credential_ref is not None
            and target.credential_ref.startswith("os-keyring:connection/")
        )

    def test_secret_is_edited_and_cascade_deleted_with_connection(self) -> None:
        target = self._target()
        self.registry.put(target)
        secret_name = target.credential_ref.split("/", 1)[1]
        self.assertEqual(self.credentials.resolve(target.credential_ref), "sek-1234567890")
        # Rotate the secret under the same name.
        self.credentials.set(secret_name, "new-secret-abcdef")
        self.assertEqual(self.credentials.resolve(target.credential_ref), "new-secret-abcdef")
        # Deleting the connection cascades the secret from the keyring via the
        # single runtime helper the TUI also uses.
        from karox.connections import remove_connection

        remove_connection(
            target.connection_id,
            registry=self.registry,
            credentials=self.credentials,
        )
        with self.assertRaises(Exception):
            self.credentials.resolve(target.credential_ref)

    def test_built_in_connections_share_one_runtime_path(self) -> None:
        # The ClickUp preset maps onto the same runtime profile as the generic
        # client -- there is no per-preset branch, only a pre-fill.
        clickup = mcp_client_preset("clickup")
        generic = mcp_client_preset("generic-mcp")
        self.assertEqual(clickup.runtime_profile, generic.runtime_profile)
        self.assertEqual(clickup.transport, generic.transport)
        # Every preset round-trips through the universal builder.
        for preset in mcp_client_presets():
            info = self.credentials.set(preset.preset_id, "sek-1234567890")
            target = build_target_from_preset(
                preset.preset_id,
                name=f"test-{preset.preset_id}",
                credential_ref=info["reference"],
                credential_fingerprint=info["fingerprint"],
                url_stability="stable" if preset.auth_scheme == "oauth" else "temporary",
                tunnel="tailscale" if preset.auth_scheme == "oauth" else preset.tunnel_default,
            )
            self.registry.put(target)
        loaded = self.registry.list()
        self.assertEqual(len(loaded), len(mcp_client_presets()))

    def test_adapt_preset_is_native_stable_tailscale_bearer_client(self) -> None:
        preset = mcp_client_preset("adapt")
        generic = mcp_client_preset("generic-mcp")
        self.assertEqual(preset.display_name, "Adapt")
        self.assertEqual(preset.status, "stable")
        self.assertEqual(preset.runtime_profile, generic.runtime_profile)
        self.assertEqual(preset.transport, "streamable_http")
        self.assertEqual(preset.auth_scheme, "bearer")
        self.assertEqual(preset.endpoint_path, "/mcp")
        self.assertEqual(preset.tunnel_default, "tailscale")
        self.assertTrue(preset.persistent_url)
        self.assertIn("KAROX_AUTHORIZATION", preset.instructions)
        self.assertIn("Personal", preset.instructions)

    def test_effective_url_is_generated_correctly_for_custom_tunnel(self) -> None:
        info = self.credentials.set("demo", "sek-1234567890")
        target = build_target_from_preset(
            "custom",
            name="custom",
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
            tunnel="custom",
            public_url="https://my-tunnel.example.com",
        )
        # The endpoint path is appended to the public URL exactly once.
        self.assertEqual(target.effective_url, "https://my-tunnel.example.com/mcp")
        # Cloudflare/tailscale URLs are not known up front (the bridge owns them).
        ephemeral = build_target_from_preset(
            "generic-mcp",
            name="eph",
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
            tunnel="cloudflare",
        )
        self.assertIsNone(ephemeral.effective_url)

    def test_endpoint_path_is_not_duplicated_in_effective_url(self) -> None:
        info = self.credentials.set("demo", "sek-1234567890")
        target = build_target_from_preset(
            "custom",
            name="custom",
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
            tunnel="custom",
            public_url="https://my-tunnel.example.com/",
        )
        self.assertEqual(target.effective_url, "https://my-tunnel.example.com/mcp")

    def test_oauth_requires_stable_url(self) -> None:
        info = self.credentials.set("demo", "sek-1234567890")
        with self.assertRaises(ConnectionConfigurationError):
            McpClientTarget(
                connection_id="c-test000000000000",
                name="oauth-temp",
                preset_id="chatgpt-web",
                transport="streamable_http",
                endpoint_path="/mcp",
                auth_scheme="oauth",
                tunnel="tailscale",
                runtime_profile="chatgpt-web",
                url_stability="temporary",
                credential_ref=info["reference"],
            )

    def test_none_auth_requires_explicit_opt_in_and_cannot_carry_a_secret(self) -> None:
        info = self.credentials.set("demo", "sek-1234567890")
        with self.assertRaises(ConnectionConfigurationError):
            McpClientTarget(
                connection_id="c-test000000000001",
                name="none-with-secret",
                preset_id="generic-mcp",
                transport="streamable_http",
                endpoint_path="/mcp",
                auth_scheme="none",
                tunnel="local",
                runtime_profile="generic-streamable-http",
                credential_ref=info["reference"],
            )
        # no-auth is only valid with no credential at all.
        ok = McpClientTarget(
            connection_id="c-test000000000002",
            name="none-ok",
            preset_id="generic-mcp",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="none",
            tunnel="local",
            runtime_profile="generic-streamable-http",
        )
        self.assertEqual(ok.auth_scheme, "none")

    def test_old_pre_versioned_config_is_migrated_in_memory(self) -> None:
        # A first cut stored a bare ``connections`` array with no schema_version.
        info = self.credentials.set("demo", "sek-1234567890")
        bare = {
            "connections": [
                {
                    "connection_id": "c-legacy0000000000",
                    "name": "Legacy ClickUp",
                    "preset_id": "clickup",
                    "transport": "streamable_http",
                    "endpoint_path": "/mcp",
                    "auth_scheme": "bearer",
                    "tunnel": "cloudflare",
                    "runtime_profile": "generic-streamable-http",
                    "credential_ref": info["reference"],
                    "credential_fingerprint": info["fingerprint"],
                }
            ]
        }
        self.path.write_text(json.dumps(bare), encoding="utf-8")
        registry = ConnectionRegistry(self.path)
        loaded = registry.list()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "Legacy ClickUp")
        # A subsequent save writes the current schema version.
        registry.put(loaded[0])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], CONNECTIONS_SCHEMA_VERSION)

    def test_corrupted_config_raises_connection_error(self) -> None:
        self.path.write_text("{not valid json", encoding="utf-8")
        registry = ConnectionRegistry(self.path)
        with self.assertRaises(ConnectionError):
            registry.list()

    def test_mask_secret_hides_the_full_value(self) -> None:
        self.assertEqual(mask_secret("sek-1234567890"), "••••••••7890")
        self.assertEqual(mask_secret(""), "")
        # Short values still mask without exposing much.
        self.assertIn("•", mask_secret("ab"))

    def test_auth_headers_match_each_scheme(self) -> None:
        info = self.credentials.set("demo", "sek-1234567890")
        bearer = build_target_from_preset(
            "generic-mcp", name="b", credential_ref=info["reference"], credential_fingerprint=info["fingerprint"]
        )
        self.assertEqual(auth_headers(bearer, "sek-1234567890"), {"Authorization": "Bearer sek-1234567890"})
        api_key = build_target_from_preset(
            "promptql", name="k", credential_ref=info["reference"], credential_fingerprint=info["fingerprint"]
        )
        self.assertEqual(auth_headers(api_key, "sek-1234567890"), {"X-API-Key": "sek-1234567890"})
        custom = build_target_from_preset(
            "custom",
            name="c",
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
            auth_scheme="custom_header",
            header_name="X-My-Token",
            header_prefix="Token ",
        )
        self.assertEqual(auth_headers(custom, "sek-1234567890"), {"X-My-Token": "Token sek-1234567890"})
        none = build_target_from_preset(
            "custom",
            name="n",
            tunnel="local",
            auth_scheme="none",
        )
        # ``none`` has no secret and no static header; the form prevented saving
        # one, so auth_headers returns nothing.
        self.assertEqual(auth_headers(none, ""), {})

    def test_credential_reference_round_trips(self) -> None:
        ref = ConnectionCredentialReference("clickup-demo")
        self.assertEqual(str(ref), "os-keyring:connection/clickup-demo")
        parsed = ConnectionCredentialReference.parse(str(ref))
        self.assertEqual(parsed.name, "clickup-demo")
        with self.assertRaises(ConnectionConfigurationError):
            ConnectionCredentialReference.parse("os-keyring:provider/other")


if __name__ == "__main__":
    unittest.main()
