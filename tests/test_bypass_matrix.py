"""One contract matrix for Bypass across every connection family.

Data-driven rather than one test per product: the point of the shared access
mode is that ChatGPT, Claude, Hyperagent, Adapt, Notion, ClickUp, PromptQL,
generic MCP, and both provider kinds behave identically, so they are asserted
from one table. Adds the shared-bridge invariants the alias between Adapt and
the ChatGPT bridge makes necessary.
"""

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from karox import tui_connections as hub
from karox.access_mode import (
    build_saved_profile_bypass,
    provider_access_profile,
    saved_profile_bypass_enabled,
    set_provider_bypass,
)
from karox.models import AccessProfile
from karox.registry import ProviderRecord
from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore

# preset id -> saved target profile. Presets whose bridge is a saved profile
# carry the mode on that profile.
SERVICE_MATRIX = (
    ("chatgpt-web", "chatgpt-web"),
    ("claude-web", "claude-web"),
    ("hyperagent-web", "hyperagent-web"),
    ("adapt", "chatgpt-web"),
    ("notion", "notion"),
)

# Record-backed families: their runtime is started from a saved connection
# record, so that record -- not a saved bridge profile -- carries the mode.
RECORD_MATRIX = ("clickup", "promptql", "web-agent", "ide", "custom", "generic-mcp")

PROVIDER_MATRIX = (
    ("builtin", "http://127.0.0.1:8000/v1", "local"),
    ("custom-api", "https://api.example.test/v1", "public"),
)


def saved(target, name, repository=None, **overrides):
    values = dict(
        name=name,
        target_profile=target,
        tools=("karox.repo.read_file", "karox.git.status"),
        access_profile=AccessProfile.WORKSPACE_WRITE,
        port=8765,
        tunnel="tailscale",
    )
    if repository is not None:
        values["repository"] = str(repository)
    values.update(overrides)
    return SavedWebBridgeProfile(**values)


class ServiceMatrixTests(unittest.TestCase):
    """add / existing / enable / persist / reopen / disable, per family."""

    def test_every_service_family_shares_one_bypass_lifecycle(self) -> None:
        for preset, target in SERVICE_MATRIX:
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as tmp:
                repository = Path(tmp).resolve()
                store = WebBridgeProfileStore(Path(tmp) / "profiles.json")
                base = saved(target, f"{preset}-matrix", repository=repository)
                store.put(base, replace_existing=False)

                # legacy record -> OFF
                self.assertFalse(saved_profile_bypass_enabled(store.get(base.name)))

                # enable -> persists -> reads back after "reopening" the store
                store.put(build_saved_profile_bypass(store.get(base.name), True))
                reopened = WebBridgeProfileStore(Path(tmp) / "profiles.json")
                stored = reopened.get(base.name)
                self.assertTrue(saved_profile_bypass_enabled(stored))
                self.assertEqual(stored.access_profile, AccessProfile.ELEVATED)

                # identity, endpoint, and credential references are untouched
                self.assertEqual(stored.name, base.name)
                self.assertEqual(stored.target_profile, base.target_profile)
                self.assertEqual(stored.port, base.port)
                self.assertEqual(stored.public_url, base.public_url)
                self.assertEqual(stored.tunnel, base.tunnel)
                self.assertEqual(
                    stored.browser_credential_refs, base.browser_credential_refs
                )

                # disable -> back to the protected contract, still one record
                reopened.put(build_saved_profile_bypass(stored, False))
                final = WebBridgeProfileStore(Path(tmp) / "profiles.json")
                self.assertFalse(saved_profile_bypass_enabled(final.get(base.name)))
                self.assertEqual(
                    final.get(base.name).access_profile,
                    AccessProfile.WORKSPACE_WRITE,
                )
                self.assertEqual(len(final.list()), 1)

    def test_no_secret_is_written_to_the_profile_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            store = WebBridgeProfileStore(path)
            base = saved("chatgpt-web", "leak-check", repository=Path(tmp).resolve())
            store.put(base, replace_existing=False)
            store.put(build_saved_profile_bypass(store.get(base.name), True))
            raw = path.read_text(encoding="utf-8").lower()
            for forbidden in ("password", "secret", "bearer", "token"):
                self.assertNotIn(forbidden, raw)


class ProviderMatrixTests(unittest.TestCase):
    def test_builtin_and_custom_providers_behave_identically(self) -> None:
        for provider_id, base_url, privacy in PROVIDER_MATRIX:
            with self.subTest(provider=provider_id):
                record = ProviderRecord(
                    provider_id=provider_id,
                    adapter_kind="openai_compatible_chat",
                    base_url=base_url,
                    privacy_class=privacy,
                    credential_ref=f"os-keyring:provider/{provider_id}",
                )
                self.assertEqual(
                    provider_access_profile(record), AccessProfile.WORKSPACE_WRITE
                )
                enabled = set_provider_bypass(record, True)
                self.assertEqual(
                    provider_access_profile(enabled), AccessProfile.ELEVATED
                )
                # persistence through the registry's own serialization
                restored = ProviderRecord.from_dict(asdict(enabled))
                self.assertEqual(
                    provider_access_profile(restored), AccessProfile.ELEVATED
                )
                self.assertEqual(restored.credential_ref, record.credential_ref)
                self.assertEqual(restored.base_url, record.base_url)
                back = ProviderRecord.from_dict(
                    asdict(set_provider_bypass(restored, False))
                )
                self.assertEqual(
                    provider_access_profile(back), AccessProfile.WORKSPACE_WRITE
                )
                self.assertEqual(back.credential_ref, record.credential_ref)


class RecordBackedMatrixTests(unittest.TestCase):
    """ClickUp, PromptQL, generic and custom MCP keep the mode on the record."""

    def _registry(self, tmp):
        from karox.connections import ConnectionRegistry

        return ConnectionRegistry(Path(tmp) / "connections.json")

    def _record(self, preset):
        from karox.connections import McpClientTarget

        return McpClientTarget(
            connection_id=f"{preset}-matrix",
            name=f"{preset} matrix",
            preset_id=preset,
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="tailscale",
            runtime_profile="generic-streamable-http",
            credential_ref="os-keyring:connection/matrix",
            port=8790,
        )

    def test_lifecycle_is_identical_for_every_record_backed_family(self) -> None:
        for preset in RECORD_MATRIX:
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as tmp:
                registry = self._registry(tmp)
                record = registry.put(self._record(preset))
                # legacy record -> OFF
                self.assertFalse(record.bypass)

                enabled = registry.set_bypass(record.connection_id, True)
                self.assertTrue(enabled.bypass)
                # identity, endpoint, credential all survive the flip
                self.assertEqual(enabled.connection_id, record.connection_id)
                self.assertEqual(enabled.credential_ref, record.credential_ref)
                self.assertEqual(enabled.public_url, record.public_url)
                self.assertEqual(enabled.port, record.port)
                self.assertEqual(enabled.endpoint_path, record.endpoint_path)

                # persists across a fresh registry object (reopen)
                reopened = self._registry(tmp)
                self.assertTrue(reopened.get(record.connection_id).bypass)

                # idempotent, and reversible
                same = reopened.set_bypass(record.connection_id, True)
                self.assertTrue(same.bypass)
                off = reopened.set_bypass(record.connection_id, False)
                self.assertFalse(off.bypass)
                self.assertEqual(off.credential_ref, record.credential_ref)
                self.assertEqual(len(reopened.list()), 1)

    def test_a_non_boolean_is_refused(self) -> None:
        from karox.connections import ConnectionConfigurationError

        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp)
            record = registry.put(self._record("clickup"))
            with self.assertRaises(ConnectionConfigurationError):
                registry.set_bypass(record.connection_id, "true")  # type: ignore[arg-type]

    def test_a_record_written_before_the_field_reads_off(self) -> None:
        from karox.connections import McpClientTarget

        payload = self._record("promptql").to_dict()
        payload.pop("bypass")
        self.assertFalse(McpClientTarget.from_dict(payload).bypass)


class SharedBridgeSemanticsTests(unittest.TestCase):
    """Logical connection and physical bridge are not the same object."""

    def test_adapt_and_chatgpt_share_one_physical_target(self) -> None:
        self.assertEqual(hub.service_target_profile("adapt"), "chatgpt-web")
        self.assertEqual(hub.service_target_profile("chatgpt-web"), "chatgpt-web")
        self.assertIn("chatgpt-web", hub.shared_bridge_presets("adapt"))
        self.assertIn("adapt", hub.shared_bridge_presets("chatgpt-web"))

    def test_unshared_families_report_no_co_tenants(self) -> None:
        for preset in ("claude-web", "hyperagent-web", "notion", "promptql"):
            with self.subTest(preset=preset):
                self.assertEqual(hub.shared_bridge_presets(preset), ())

    def test_every_family_offers_the_toggle(self) -> None:
        for preset in [item[0] for item in SERVICE_MATRIX] + list(RECORD_MATRIX):
            with self.subTest(preset=preset):
                self.assertTrue(hub.bypass_supported(preset))

    def test_record_backed_families_never_share_a_physical_bridge(self) -> None:
        # Each starts its own runtime from its own record, so widening one
        # cannot widen another.
        for preset in RECORD_MATRIX:
            with self.subTest(preset=preset):
                self.assertIsNone(hub.saved_bridge_target(preset))
                self.assertEqual(hub.shared_bridge_presets(preset), ())

    def test_deleting_the_adapt_binding_cannot_delete_the_shared_bridge(self) -> None:
        # The saved-profile delete path is reserved for presets that own their
        # bridge; Adapt is an alias user, so its delete can only remove its own
        # connection record.
        self.assertFalse(hub.owns_saved_bridge("adapt"))
        for preset in ("chatgpt-web", "claude-web", "hyperagent-web", "notion"):
            with self.subTest(preset=preset):
                self.assertTrue(hub.owns_saved_bridge(preset))
        for preset in RECORD_MATRIX:
            with self.subTest(preset=preset):
                self.assertFalse(hub.owns_saved_bridge(preset))


class HyperagentMigrationTests(unittest.TestCase):
    def test_legacy_full_access_record_reads_as_bypass_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp).resolve()
            legacy = saved(
                "hyperagent-web",
                "legacy-full",
                repository=repository,
                access_profile=AccessProfile.ELEVATED,
                browser_external_https=True,
                browser_headed=True,
                browser_user_takeover=True,
                browser_network_inspection=True,
            )
            self.assertTrue(saved_profile_bypass_enabled(legacy))
            self.assertTrue(hub.saved_profile_full_access_enabled(legacy))

    def test_record_written_before_the_field_existed_reads_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = saved(
                "hyperagent-web", "legacy-plain", repository=Path(tmp).resolve()
            )
            self.assertFalse(saved_profile_bypass_enabled(legacy))
            self.assertFalse(hub.saved_profile_full_access_enabled(legacy))


if __name__ == "__main__":
    unittest.main()
