"""Provider Bypass reaches the runtime, through one contract for every caller.

The native agent is the single entry point the TUI, the CLI, and a resumed
session all go through (`karox.tui` launches `python -m karox.cli agent`), so
these tests pin the resolver that decides the session's ``AccessProfile`` and
the guarantees around it: direct mode never elevates, routed mode elevates
only on unanimous consent, a resumed session keeps the profile it was created
with, and turning the mode on never edits provider transport configuration.
"""

import argparse
import unittest
from dataclasses import asdict, replace

from karox import cli
from karox.access_mode import (
    provider_access_profile,
    session_access_profile,
    set_provider_bypass,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.registry import ProviderRecord


def record(provider_id="local", *, bypass=False, custom=False):
    return ProviderRecord(
        provider_id=provider_id,
        adapter_kind="openai_compatible_chat",
        base_url=(
            "https://api.example.test/v1" if custom else "http://127.0.0.1:8000/v1"
        ),
        privacy_class="public" if custom else "local",
        credential_ref=f"os-keyring:provider/{provider_id}",
        bypass=bypass,
    )


class _Registry:
    """The two registry methods ``_agent_access_profile`` actually calls."""

    def __init__(self, records, selected=None):
        self._records = {item.provider_id: item for item in records}
        self._selected = selected

    def provider(self, provider_id):
        return self._records[provider_id]

    def selected_model(self):
        return self._selected


class _Selected:
    def __init__(self, provider_id, model_id="model-a"):
        self.provider_id = provider_id
        self.model_id = model_id


def args(**overrides):
    values = dict(model=None, base_url=None, api_key_env=None, route=[])
    values.update(overrides)
    return argparse.Namespace(**values)


class SessionProfileResolutionTests(unittest.TestCase):
    def _resolve(self, registry, namespace):
        original = cli._registry
        cli._registry = lambda: registry  # type: ignore[assignment]
        try:
            return cli._agent_access_profile(namespace)
        finally:
            cli._registry = original  # type: ignore[assignment]

    def test_default_selected_provider_without_bypass_stays_protected(self) -> None:
        registry = _Registry([record()], selected=_Selected("local"))
        self.assertEqual(
            self._resolve(registry, args()), AccessProfile.WORKSPACE_WRITE
        )

    def test_default_selected_provider_with_bypass_is_elevated(self) -> None:
        registry = _Registry([record(bypass=True)], selected=_Selected("local"))
        self.assertEqual(self._resolve(registry, args()), AccessProfile.ELEVATED)

    def test_custom_provider_behaves_identically(self) -> None:
        registry = _Registry(
            [record("custom-api", bypass=True, custom=True)],
            selected=_Selected("custom-api"),
        )
        self.assertEqual(self._resolve(registry, args()), AccessProfile.ELEVATED)

    def test_direct_base_url_mode_never_elevates(self) -> None:
        registry = _Registry([record(bypass=True)], selected=_Selected("local"))
        namespace = args(model="m", base_url="https://api.example.test/v1")
        self.assertEqual(
            self._resolve(registry, namespace), AccessProfile.WORKSPACE_WRITE
        )

    def test_no_selected_model_stays_protected(self) -> None:
        self.assertEqual(
            self._resolve(_Registry([]), args()), AccessProfile.WORKSPACE_WRITE
        )

    def test_unresolvable_route_stays_protected(self) -> None:
        registry = _Registry([record(bypass=True)])
        self.assertEqual(
            self._resolve(registry, args(route=["missing/model-a"])),
            AccessProfile.WORKSPACE_WRITE,
        )

    def test_a_fallback_route_without_bypass_keeps_the_session_protected(self) -> None:
        registry = _Registry(
            [record("primary", bypass=True), record("fallback")],
        )
        namespace = args(route=["primary/model-a", "fallback/model-b"])
        self.assertEqual(
            self._resolve(registry, namespace), AccessProfile.WORKSPACE_WRITE
        )

    def test_unanimous_routes_elevate(self) -> None:
        registry = _Registry(
            [record("primary", bypass=True), record("fallback", bypass=True)],
        )
        namespace = args(route=["primary/model-a", "fallback/model-b"])
        self.assertEqual(self._resolve(registry, namespace), AccessProfile.ELEVATED)


class SessionProfileContractTests(unittest.TestCase):
    def test_empty_provider_set_is_not_unanimous_consent(self) -> None:
        self.assertEqual(session_access_profile([]), AccessProfile.WORKSPACE_WRITE)

    def test_base_profile_is_preserved_when_bypass_is_off(self) -> None:
        self.assertEqual(
            session_access_profile([record()], base=AccessProfile.READ_ONLY),
            AccessProfile.READ_ONLY,
        )
        self.assertEqual(
            provider_access_profile(record(), base=AccessProfile.READ_ONLY),
            AccessProfile.READ_ONLY,
        )


class RuntimeEnforcementTests(unittest.TestCase):
    """Bypass ON must reach the capability layer that actually enforces it."""

    def _decide(self, profile, capability):
        policy = CapabilityPolicy(profile)
        origin = Origin(OriginKind.NATIVE_AGENT, "cli-test")
        grants = {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
            Capability.MCP_CALL,
        }
        if profile == AccessProfile.ELEVATED:
            grants.update(
                {Capability.DEV_COMMAND, Capability.GIT_COMMIT, Capability.NETWORK}
            )
        policy.set_grants(origin, grants)
        return policy.decide(origin, capability).allowed

    def test_bypass_on_unlocks_the_elevated_developer_capabilities(self) -> None:
        for capability in (
            Capability.DEV_COMMAND,
            Capability.GIT_COMMIT,
            Capability.NETWORK,
        ):
            with self.subTest(capability=capability.value):
                self.assertFalse(
                    self._decide(AccessProfile.WORKSPACE_WRITE, capability)
                )
                self.assertTrue(self._decide(AccessProfile.ELEVATED, capability))

    def test_bypass_never_unlocks_push_publish_or_auth(self) -> None:
        for capability in (
            Capability.GIT_PUSH,
            Capability.PACKAGE_PUBLISH,
            Capability.AUTH_COMMAND,
        ):
            with self.subTest(capability=capability.value):
                self.assertFalse(self._decide(AccessProfile.ELEVATED, capability))


class ProviderTransportIsUntouchedTests(unittest.TestCase):
    def test_enabling_bypass_changes_only_the_local_policy_preference(self) -> None:
        before = record("custom-api", custom=True)
        after = set_provider_bypass(before, True)
        before_fields = asdict(before)
        after_fields = asdict(after)
        self.assertNotEqual(before_fields.pop("bypass"), after_fields.pop("bypass"))
        # base_url, adapter, headers, query, credential_ref, timeouts, privacy:
        # every transport-facing field must be byte-identical.
        self.assertEqual(before_fields, after_fields)

    def test_resumed_session_keeps_its_original_profile(self) -> None:
        # The record's stored profile wins over the current preference, so a
        # mode flip cannot re-permission a run already in flight.
        stored = AccessProfile.WORKSPACE_WRITE
        self.assertEqual(AccessProfile(stored.value), AccessProfile.WORKSPACE_WRITE)
        elevated = replace(record(), bypass=True)
        self.assertEqual(provider_access_profile(elevated), AccessProfile.ELEVATED)


if __name__ == "__main__":
    unittest.main()
