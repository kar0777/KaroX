"""Regression tests for Blocker 3: ``browser.input`` must imply ``browser.read``.

The contradiction ``browser_permission.read=false, input=true`` arose because
the tool bundle, the granted capabilities and the diagnostics payload each
derived ``read``/``input`` independently.  A bundle with only the input tools
(open/click/fill/select/press/close) and none of the read tools
(snapshot/get_text/console/network_failures/screenshot) passed the capability
gate -- ``BROWSER_INPUT`` was granted and allowed under WORKSPACE_WRITE -- but
left ``BROWSER_READ`` un-granted, so ``web_bridge_diagnostics`` reported
``read:false, input:true`` and a hosted client could click a button but never
snapshot the result.

The fix has two layers that this module pins:

  1. ``WebBridgeConnectConfig.__post_init__`` normalizes the tool bundle so the
     read tools are always present whenever any input tool is (input
     auto-includes read).
  2. ``CapabilityPolicy.decide`` is a capability-level backstop: a request for
     ``BROWSER_READ`` is allowed whenever ``BROWSER_INPUT`` is effectively
     granted, even if a future caller assembles grants without going through the
     config normalization.

These tests cover the requested scenarios:
  - input without read does not create a contradictory session;
  - input auto-includes read (or the validator rejects);
  - snapshot only when ``browser.read``, click only when ``browser.input``;
  - diagnostics reflects effective capabilities, not raw checkbox values;
  - TUI -> argv -> parsed profile -> runtime -> diagnostics end-to-end.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Set

from _support import (  # noqa: F401 - inserts src on sys.path
    SRC,
    _CONFIG_OVERRIDES,
    _LEGACY_OVERRIDES,
    _RUNTIME_OVERRIDES,
    child_environment,
)

import karox.tui as tui
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy


def _hosted_origin(session_id: str) -> Origin:
    return Origin(OriginKind.HOSTED_CLIENT, f"chatgpt-web-tools-{session_id}")


def _config(tools: tuple[str, ...]) -> "object":
    from karox.web_bridge_launcher import WebBridgeConnectConfig

    return WebBridgeConnectConfig(
        profile="chatgpt-web",
        repository=Path.cwd(),
        port=9903,
        tools=tools,
        access_profile=AccessProfile.WORKSPACE_WRITE,
    )


_READ_TOOLS = (
    "karox.browser.snapshot",
    "karox.browser.get_text",
    "karox.browser.console",
    "karox.browser.network_failures",
    "karox.browser.screenshot",
)
_INPUT_TOOLS = (
    "karox.browser.open",
    "karox.browser.click",
    "karox.browser.fill",
    "karox.browser.select",
    "karox.browser.press",
    "karox.browser.close",
)


class BrowserBundleNormalizationTests(unittest.TestCase):
    """Layer 1: the tool bundle is normalized so input implies read."""

    def test_input_without_read_auto_includes_read_tools(self) -> None:
        # A contradictory bundle (input tools only, no read tools) is normalized
        # so the read tools are present in the effective tool bundle.  A session
        # is never left in a state where input is permitted but read is not.
        config = _config(
            ("karox.repo.read_file",) + _INPUT_TOOLS
        )
        for read_tool in _READ_TOOLS:
            self.assertIn(
                read_tool,
                config.tools,
                f"{read_tool} must be auto-included when a browser input tool is selected",
            )

    def test_input_only_bundle_still_validates_unknown_tools(self) -> None:
        # The normalization must not weaken the unknown-tool check: an unknown
        # tool alongside input tools is still rejected.
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with self.assertRaises(ValueError):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                port=9905,
                tools=("karox.repo.read_file", "karox.browser.click", "karox.bogus.tool"),
                access_profile=AccessProfile.WORKSPACE_WRITE,
            )

    def test_read_only_bundle_does_not_leak_input_tools(self) -> None:
        # The implication is one-directional: read does NOT imply input.  A
        # bundle with only read tools (no open/click/...) stays read-only; the
        # input tools are NOT auto-added.
        config = _config(
            ("karox.repo.read_file",) + _READ_TOOLS
        )
        for input_tool in _INPUT_TOOLS:
            self.assertNotIn(input_tool, config.tools)

    def test_normalization_preserves_caller_order_then_appends_read(self) -> None:
        # Caller-selected tools keep their order; auto-included read tools are
        # appended in canonical order so the bundle is deterministic.
        config = _config(
            ("karox.repo.read_file", "karox.browser.click", "karox.browser.open")
        )
        tools = list(config.tools)
        self.assertEqual(tools[0], "karox.repo.read_file")
        self.assertEqual(tools[1], "karox.browser.click")
        self.assertEqual(tools[2], "karox.browser.open")
        # every read tool appears exactly once, after the input tools
        for read_tool in _READ_TOOLS:
            self.assertEqual(tools.count(read_tool), 1)

    def test_bundle_with_read_and_input_is_unchanged(self) -> None:
        # If the read tools are already present, normalization is a no-op: no
        # duplicates are introduced.
        config = _config(
            ("karox.repo.read_file",) + _READ_TOOLS + _INPUT_TOOLS
        )
        self.assertEqual(len(config.tools), len(set(config.tools)))


class DiagnosticsReflectsEffectiveCapabilitiesTests(unittest.TestCase):
    """Layer 1 (diagnostics): diagnostics reflects the effective capability
    set, not raw checkbox values."""

    def test_input_only_bundle_diagnostics_reports_read_true(self) -> None:
        from karox.web_bridge_launcher import web_bridge_diagnostics

        config = _config(
            ("karox.repo.read_file", "karox.browser.open", "karox.browser.click")
        )
        diag = web_bridge_diagnostics(config)
        bp = diag["browser_permission"]
        self.assertTrue(bp["read"], "input must imply read in diagnostics")
        self.assertTrue(bp["input"])
        self.assertNotEqual(bp["read"], False)
        # the auto-included read tools appear in available_tools, not disabled
        self.assertIn("karox.browser.snapshot", diag["available_tools"])
        self.assertIn("karox.browser.get_text", diag["available_tools"])
        snapshot_disabled = [
            d for d in diag["disabled_tools"] if d["name"] == "karox.browser.snapshot"
        ]
        self.assertEqual(snapshot_disabled, [])

    def test_read_only_bundle_diagnostics_reports_input_false(self) -> None:
        from karox.web_bridge_launcher import web_bridge_diagnostics

        config = _config(
            ("karox.repo.read_file",) + _READ_TOOLS
        )
        diag = web_bridge_diagnostics(config)
        bp = diag["browser_permission"]
        self.assertTrue(bp["read"])
        self.assertFalse(bp["input"])


class CapabilityGateTests(unittest.TestCase):
    """Layer 2: the capability gate keeps snapshot/read and click/input on
    distinct tiers, and the policy backstop enforces input-implies-read."""

    def test_snapshot_requires_read_click_requires_input(self) -> None:
        from karox.hosted_tools_runtime import _HOSTED_EXTRA_TOOLS

        origin = _hosted_origin("snapshot-vs-click")

        # only read granted: snapshot allowed, click denied
        read_grants: Set[Capability] = set()
        meta = _HOSTED_EXTRA_TOOLS["karox.browser.snapshot"]
        if meta.capability is not None:
            read_grants.add(meta.capability)
        policy_ro = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy_ro.set_grants(origin, read_grants)
        self.assertTrue(policy_ro.decide(origin, Capability.BROWSER_READ).allowed)
        self.assertFalse(policy_ro.decide(origin, Capability.BROWSER_INPUT).allowed)

        # only input granted: click allowed, and read allowed via the backstop
        input_grants: Set[Capability] = set()
        meta = _HOSTED_EXTRA_TOOLS["karox.browser.click"]
        if meta.capability is not None:
            input_grants.add(meta.capability)
        policy_in = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy_in.set_grants(origin, input_grants)
        self.assertTrue(policy_in.decide(origin, Capability.BROWSER_INPUT).allowed)
        self.assertTrue(
            policy_in.decide(origin, Capability.BROWSER_READ).allowed,
            "input must imply read even when read was not explicitly granted",
        )

    def test_policy_input_implies_read_when_grants_bypass_normalization(self) -> None:
        # If a caller assembles grants directly (bypassing the config
        # normalization), the capability-level backstop still holds: a request
        # for BROWSER_READ is allowed whenever BROWSER_INPUT is effectively
        # granted under WORKSPACE_WRITE, so no code path reaches the
        # contradictory read:false, input:true state.
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        origin = _hosted_origin("backstop")
        policy.set_grants(origin, {Capability.BROWSER_INPUT})
        self.assertTrue(policy.decide(origin, Capability.BROWSER_INPUT).allowed)
        decision = policy.decide(origin, Capability.BROWSER_READ)
        self.assertTrue(decision.allowed, "input must imply read at the policy layer")
        self.assertIn("input implies read", decision.reason)

    def test_input_under_read_only_profile_still_rejected(self) -> None:
        # The implication must not loosen the profile gate: under READ_ONLY,
        # BROWSER_INPUT is not in the profile baseline, so even a direct grant
        # is denied (and read is NOT silently upgraded either, since input
        # itself was rejected).
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        origin = _hosted_origin("read-only-input")
        policy.set_grants(origin, {Capability.BROWSER_INPUT})
        self.assertFalse(policy.decide(origin, Capability.BROWSER_INPUT).allowed)
        self.assertFalse(policy.decide(origin, Capability.BROWSER_READ).allowed)


class TuiArgvToDiagnosticsEndToEndTests(unittest.TestCase):
    """The full Blocker-3 chain end-to-end: TUI checkbox selection -> launch
    argv -> parsed bridge config -> diagnostics reflecting effective
    capabilities (read:true when input:true)."""

    def test_browser_input_only_selection_reports_read_true_end_to_end(self) -> None:
        from karox.cli import _parser
        from karox.web_bridge_launcher import (
            WebBridgeConnectConfig,
            web_bridge_diagnostics,
        )

        # User selects browser.input tools WITHOUT any explicit browser.read
        # tools, plus repo write (which forces WORKSPACE_WRITE).  After the
        # bridge argv is parsed back into a config and diagnostics is computed,
        # read must be true (input implies read) and the read tools must appear
        # in available_tools.
        setup = tui.BridgeSetup(
            "chatgpt-web",
            9904,
            (
                "karox.repo.read_file",
                "karox.repo.write_file",
            )
            + _INPUT_TOOLS,
            tunnel_provider="cloudflare",
        )
        launch = tui._managed_web_bridge_launch(Path.cwd(), setup)
        self.assertIn("--write", launch.argv)

        argv = list(launch.argv)
        while argv and argv[0] != "bridge":
            argv.pop(0)
        args = _parser().parse_args(argv)
        self.assertTrue(args.write)

        config = WebBridgeConnectConfig(
            profile=args.profile,
            repository=Path.cwd(),
            port=int(args.port),
            tools=tuple(args.tool),
            access_profile=(
                AccessProfile.WORKSPACE_WRITE if args.write else AccessProfile.READ_ONLY
            ),
            tunnel=args.tunnel,
        )
        diag = web_bridge_diagnostics(config)
        bp = diag["browser_permission"]
        self.assertTrue(bp["read"], "input selection must report read:true end-to-end")
        self.assertTrue(bp["input"])
        self.assertIn("karox.browser.snapshot", diag["available_tools"])
        self.assertIn("karox.browser.get_text", diag["available_tools"])
        self.assertEqual(bp["localhost_only"], True)


if __name__ == "__main__":
    unittest.main()
