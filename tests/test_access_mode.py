"""The shared Bypass (access mode) contract.

Property tests over ``karox.access_mode``: default OFF for legacy records,
per-connection persistence, mapping onto the existing Elevated capability
contract, Hyperagent legacy equivalence, and the no-rotation guarantees
(credentials, identity, URL) that a mode flip must never violate.
"""

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from karox.access_mode import (
    BYPASS_TOOL_NAMES,
    build_saved_profile_bypass,
    provider_access_profile,
    provider_bypass_enabled,
    saved_profile_bypass_enabled,
    set_provider_bypass,
)
from karox.models import AccessProfile
from karox.registry import ProviderRecord
from karox.web_bridge_profiles import SavedWebBridgeProfile

# Every non-Hyperagent saved-bridge family shares one minimal toggle;
# Hyperagent layers its historical browser contract on top of the same mode.
SAVED_TARGETS = ("chatgpt-web", "claude-web", "notion", "adapt")


def provider(**overrides):
    values = dict(
        provider_id="local",
        adapter_kind="openai_compatible_chat",
        base_url="http://127.0.0.1:8000/v1",
        privacy_class="local",
        credential_ref="os-keyring:provider/local-test",
    )
    values.update(overrides)
    return ProviderRecord(**values)


def saved(target="adapt", **overrides):
    values = dict(
        name=f"{target}-bypass-test",
        target_profile=target,
        tools=("karox.repo.read_file", "karox.git.status"),
        access_profile=AccessProfile.WORKSPACE_WRITE,
        port=8769,
        tunnel="tailscale",
    )
    values.update(overrides)
    return SavedWebBridgeProfile(**values)


class ProviderBypassTests(unittest.TestCase):
    def test_legacy_record_without_the_field_reads_off(self) -> None:
        record = ProviderRecord.from_dict(
            {
                "provider_id": "legacy",
                "adapter_kind": "openai_compatible_chat",
                "base_url": "http://127.0.0.1:8000/v1",
                "privacy_class": "local",
            }
        )
        self.assertFalse(provider_bypass_enabled(record))

    def test_preference_round_trips_through_serialization(self) -> None:
        record = set_provider_bypass(provider(), True)
        self.assertTrue(provider_bypass_enabled(record))
        restored = ProviderRecord.from_dict(asdict(record))
        self.assertTrue(provider_bypass_enabled(restored))
        self.assertTrue(asdict(record)["bypass"])

    def test_non_boolean_flag_is_rejected_not_coerced(self) -> None:
        with self.assertRaises(ValueError):
            ProviderRecord.from_dict(
                {
                    "provider_id": "legacy",
                    "adapter_kind": "openai_compatible_chat",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "privacy_class": "local",
                    "bypass": "true",
                }
            )

    def test_toggle_never_touches_identity_or_credential(self) -> None:
        before = provider()
        after = set_provider_bypass(before, True)
        self.assertEqual(after.provider_id, before.provider_id)
        self.assertEqual(after.credential_ref, before.credential_ref)
        self.assertEqual(after.base_url, before.base_url)
        self.assertEqual(after.headers, before.headers)
        self.assertEqual(after.adapter_kind, before.adapter_kind)
        back = set_provider_bypass(after, False)
        self.assertEqual(back.credential_ref, before.credential_ref)
        self.assertFalse(provider_bypass_enabled(back))

    def test_noop_toggle_returns_the_same_record(self) -> None:
        record = provider()
        self.assertIs(set_provider_bypass(record, False), record)

    def test_session_profile_maps_to_the_existing_elevated_contract(self) -> None:
        self.assertEqual(
            provider_access_profile(set_provider_bypass(provider(), True)),
            AccessProfile.ELEVATED,
        )
        self.assertEqual(
            provider_access_profile(provider()), AccessProfile.WORKSPACE_WRITE
        )
        self.assertEqual(
            provider_access_profile(provider(), base=AccessProfile.READ_ONLY),
            AccessProfile.READ_ONLY,
        )


class SavedProfileBypassTests(unittest.TestCase):
    def test_defaults_off_for_every_family(self) -> None:
        for target in SAVED_TARGETS:
            with self.subTest(target=target):
                self.assertFalse(saved_profile_bypass_enabled(saved(target)))

    def test_on_maps_to_elevated_and_off_returns_to_project_access(self) -> None:
        for target in SAVED_TARGETS:
            with self.subTest(target=target):
                on = build_saved_profile_bypass(saved(target), True)
                self.assertEqual(on.access_profile, AccessProfile.ELEVATED)
                self.assertTrue(saved_profile_bypass_enabled(on))
                off = build_saved_profile_bypass(on, False)
                self.assertEqual(off.access_profile, AccessProfile.WORKSPACE_WRITE)
                self.assertFalse(saved_profile_bypass_enabled(off))

    def test_toggle_keeps_connection_identity(self) -> None:
        for target in SAVED_TARGETS:
            with self.subTest(target=target):
                base = saved(target)
                on = build_saved_profile_bypass(base, True)
                self.assertEqual(on.name, base.name)
                self.assertEqual(on.target_profile, base.target_profile)
                self.assertEqual(on.port, base.port)
                self.assertEqual(on.tunnel, base.tunnel)
                self.assertEqual(on.public_url, base.public_url)
                self.assertEqual(
                    on.browser_credential_refs, base.browser_credential_refs
                )
                # Non-Hyperagent families never gain browser powers from Bypass.
                self.assertEqual(
                    on.browser_external_https, base.browser_external_https
                )
                self.assertEqual(on.browser_headed, base.browser_headed)

    def test_tool_catalogue_is_extended_once_and_stays_stable(self) -> None:
        base = saved("adapt")
        on = build_saved_profile_bypass(base, True)
        for name in BYPASS_TOOL_NAMES:
            self.assertIn(name, on.tools)
        off = build_saved_profile_bypass(on, False)
        # Clients cache the MCP tool catalogue; only the permission profile
        # changes on a toggle, never the advertised tool names.
        self.assertEqual(off.tools, on.tools)


class HyperagentEquivalenceTests(unittest.TestCase):
    def test_legacy_full_access_is_the_same_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = saved(
                "hyperagent-web",
                repository=str(Path(tmp).resolve()),
            )
            on = build_saved_profile_bypass(base, True)
            self.assertEqual(on.access_profile, AccessProfile.ELEVATED)
            self.assertTrue(on.browser_external_https)
            self.assertTrue(on.browser_headed)
            self.assertTrue(on.browser_user_takeover)
            self.assertTrue(on.browser_network_inspection)
            self.assertFalse(on.browser_payment_confirmation)
            self.assertTrue(saved_profile_bypass_enabled(on))
            off = build_saved_profile_bypass(on, False)
            self.assertEqual(off.access_profile, AccessProfile.WORKSPACE_WRITE)
            self.assertFalse(saved_profile_bypass_enabled(off))
            # The catalogue stays stable across the flip for cached clients.
            self.assertEqual(off.tools, on.tools)

    def test_tui_wrappers_stay_compatible(self) -> None:
        from karox.tui_connections import (
            build_saved_profile_full_access,
            saved_profile_full_access_enabled,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = saved(
                "hyperagent-web",
                repository=str(Path(tmp).resolve()),
            )
            on = build_saved_profile_full_access(base, True)
            self.assertTrue(saved_profile_full_access_enabled(on))
            self.assertTrue(saved_profile_bypass_enabled(on))
        # The compatibility API keeps its Hyperagent-only contract.
        with self.assertRaises(ValueError):
            build_saved_profile_full_access(saved("adapt"), True)
        # An elevated non-Hyperagent profile is Bypass, not "full access".
        elevated = build_saved_profile_bypass(saved("adapt"), True)
        self.assertFalse(saved_profile_full_access_enabled(elevated))


if __name__ == "__main__":
    unittest.main()
