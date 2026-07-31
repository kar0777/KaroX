"""Regression tests for the ``browser.input`` capability desync.

The TUI checkbox surface and the internal ``CapabilityPolicy`` profile
baselines used to disagree: the TUI added ``karox.browser.open``/``click``/
``fill`` to the tool allowlist and passed ``--write`` (WORKSPACE_WRITE), but
the policy only granted ``BROWSER_INPUT`` from the ELEVATED profile.  The
bridge therefore exited with code 2: ``session profile does not allow
browser.input`` even though the checkbox was on.

These tests pin the corrected permission model:
  - browser read is non-mutating observation (READ_ONLY+);
  - browser input is a workspace-scoped UI mutation (WORKSPACE_WRITE+);
  - git.commit / desktop.input stay ELEVATED-only by design.

They also exercise the full chain the original bug slipped through: TUI
checkbox selection -> generated launch argv -> parsed bridge config ->
effective session capabilities -> exposed tool names.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class PolicyBaselineTests(unittest.TestCase):
    """The corrected profile baselines must align with the TUI permission
    model: browser read is observation, browser input is a workspace mutation,
    git.commit stays ELEVATED-only."""

    def setUp(self) -> None:
        self.hosted = _hosted_origin("baseline")

    def test_browser_input_allowed_under_workspace_write_when_granted(self) -> None:
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(
            self.hosted,
            {Capability.BROWSER_READ, Capability.BROWSER_INPUT, Capability.PROCESS_RUN},
        )
        self.assertTrue(policy.decide(self.hosted, Capability.BROWSER_INPUT).allowed)
        self.assertTrue(policy.decide(self.hosted, Capability.BROWSER_READ).allowed)

    def test_browser_input_denied_under_workspace_write_without_grant(self) -> None:
        # ``--write`` alone must NOT enable browser input -- the explicit
        # checkbox selection is the source of permission, not the write flag.
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(self.hosted, {Capability.REPO_READ, Capability.PROCESS_RUN})
        self.assertFalse(policy.decide(self.hosted, Capability.BROWSER_INPUT).allowed)

    def test_browser_read_allowed_under_read_only_when_granted(self) -> None:
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        policy.set_grants(self.hosted, {Capability.BROWSER_READ})
        self.assertTrue(policy.decide(self.hosted, Capability.BROWSER_READ).allowed)

    def test_browser_input_denied_under_read_only_even_when_granted(self) -> None:
        # No write permission -> no browser input, even if a caller tried to
        # grant it.  This is the "validation before launching the child bridge"
        # guarantee for case 2.
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        policy.set_grants(self.hosted, {Capability.BROWSER_INPUT})
        self.assertFalse(policy.decide(self.hosted, Capability.BROWSER_INPUT).allowed)

    def test_git_commit_stays_elevated_only(self) -> None:
        # git.commit must NOT be reachable from WORKSPACE_WRITE -- it stays
        # ELEVATED-only by design (regression guard).
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(self.hosted, {Capability.GIT_COMMIT})
        self.assertFalse(policy.decide(self.hosted, Capability.GIT_COMMIT).allowed)
        elevated = CapabilityPolicy(AccessProfile.ELEVATED)
        elevated.set_grants(self.hosted, {Capability.GIT_COMMIT})
        self.assertTrue(elevated.decide(self.hosted, Capability.GIT_COMMIT).allowed)


class HostedRuntimeCapabilityGateTests(unittest.TestCase):
    """The HostedToolsRuntime constructor (the real exit-2 site) must accept
    browser.input under WORKSPACE_WRITE and reject it under READ_ONLY."""

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.tmp = self._stack.enter_context(tempfile.TemporaryDirectory())
        # Isolate config + runtime dirs so a real ~/.karox never interferes.
        cfg = Path(self.tmp) / "config"
        rt = Path(self.tmp) / "runtime"
        cfg.mkdir(); rt.mkdir()
        env = {k: str(cfg) if k in _CONFIG_OVERRIDES
               else str(rt) if k in _RUNTIME_OVERRIDES
               else ""
               for k in (*_CONFIG_OVERRIDES, *_RUNTIME_OVERRIDES)}
        # also clear legacy overrides so nothing inherited leaks in
        env.update({k: "" for k in _LEGACY_OVERRIDES})
        real_env = os.environ.copy()
        real_env.update(env)
        self._stack.enter_context(patch.dict(os.environ, real_env, clear=False))
        self.repository = Path(self.tmp) / "repo"
        self.repository.mkdir()
        from karox.sessions import SessionStore
        self.sessions = SessionStore(Path(self.tmp) / "sessions")

    def tearDown(self) -> None:
        self._stack.close()

    def _make(self, session_id, profile, tools):
        from karox.hosted_tools_runtime import HostedToolsRuntime
        self.sessions.create(self.repository, "test", profile, session_id=session_id)
        return HostedToolsRuntime(
            self.repository,
            self.sessions,
            session_id,
            tools,
            access_profile=profile,
            hosted_origin=_hosted_origin(session_id),
        )

    def test_browser_input_runtime_constructs_on_workspace_write(self) -> None:
        tools = [
            "karox.browser.open", "karox.browser.snapshot", "karox.browser.click",
            "karox.browser.fill", "karox.browser.select", "karox.browser.press",
            "karox.browser.close",
        ]
        rt = self._make("ww", AccessProfile.WORKSPACE_WRITE, tools)
        names = {d.name for d in rt.descriptors()}
        for t in tools:
            self.assertIn(t, names)
        self.assertIn("karox.browser.click", names)

    def test_browser_input_runtime_rejected_on_read_only(self) -> None:
        from karox.hosted_bridge import HostedBridgeAccessDenied
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "session profile does not allow browser.input"
        ):
            self._make("ro", AccessProfile.READ_ONLY, ["karox.browser.click"])

    def test_browser_read_only_runtime_constructs_on_read_only(self) -> None:
        tools = [
            "karox.browser.snapshot", "karox.browser.get_text",
            "karox.browser.screenshot", "karox.browser.console",
            "karox.browser.network_failures",
        ]
        rt = self._make("ro-read", AccessProfile.READ_ONLY, tools)
        names = {d.name for d in rt.descriptors()}
        for t in tools:
            self.assertIn(t, names)
        self.assertNotIn("karox.browser.click", names)

    def test_dev_server_start_stop_constructs_on_workspace_write(self) -> None:
        from karox.hosted_tools_runtime import default_server_profiles
        from karox.hosted_bridge import HostedBridgeAccessDenied
        # dev_server.start/stop map to PROCESS_RUN which is in WORKSPACE_WRITE.
        tools = ["karox.dev_server.start", "karox.dev_server.status",
                 "karox.dev_server.logs", "karox.dev_server.stop"]
        rt = self._make("ww-dev", AccessProfile.WORKSPACE_WRITE, tools)
        names = {d.name for d in rt.descriptors()}
        for t in tools:
            self.assertIn(t, names)
        # And it is still rejected on READ_ONLY (PROCESS_RUN not in READ_ONLY).
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "session profile does not allow process.run"
        ):
            self._make("ro-dev", AccessProfile.READ_ONLY, ["karox.dev_server.start"])


class TuiToCapabilitiesEndToEndTests(unittest.TestCase):
    """The chain the original bug slipped through: checkbox selection ->
    generated launch argv -> parsed bridge config -> effective session
    capabilities -> exposed tool names."""

    def test_browser_input_checkbox_yields_allowed_capabilities(self) -> None:
        # Simulate the user's exact selection: browser.read + browser.input
        # + repo write (which forces WORKSPACE_WRITE).
        setup = tui.BridgeSetup(
            "chatgpt-web",
            9901,
            (
                "karox.repo.read_file",
                "karox.browser.snapshot",
                "karox.browser.screenshot",
                "karox.browser.open",
                "karox.browser.click",
                "karox.browser.fill",
                "karox.browser.select",
                "karox.browser.press",
                "karox.browser.close",
            ),
            tunnel_provider="cloudflare",
        )
        launch = tui._managed_web_bridge_launch(Path.cwd(), setup)
        self.assertIn("--write", launch.argv)

        # Parse the generated argv back through the same parser the bridge
        # subprocess uses, so the test proves the capability gate would now
        # pass on the real launched process (not just on a hand-built config).
        from karox.cli import _parser
        argv = list(launch.argv)
        # strip the interpreter + "-m karox.cli" prefix argparse never sees
        while argv and argv[0] != "bridge":
            argv.pop(0)
        args = _parser().parse_args(argv)
        self.assertEqual(args.command, "bridge")
        self.assertEqual(args.bridge_command, "connect")
        self.assertTrue(args.write)
        self.assertIn("karox.browser.open", args.tool)

        # Build the effective session capabilities from the selected tools and
        # the WORKSPACE_WRITE profile, exactly as HostedToolsRuntime does.
        from karox.hosted_tools_runtime import _HOSTED_EXTRA_TOOLS
        extra_tools = [n for n in args.tool if n in _HOSTED_EXTRA_TOOLS]
        grants: set[Capability] = set()
        for name in extra_tools:
            meta = _HOSTED_EXTRA_TOOLS[name]
            if meta.capability is not None:
                grants.add(meta.capability)
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        origin = _hosted_origin("e2e")
        policy.set_grants(origin, grants)
        for cap in grants:
            self.assertTrue(
                policy.decide(origin, cap).allowed,
                f"{cap.value} rejected under WORKSPACE_WRITE",
            )
        self.assertIn(Capability.BROWSER_INPUT, grants)
        self.assertIn(Capability.BROWSER_READ, grants)

    def test_least_privilege_repo_write_without_browser_input(self) -> None:
        # Repository write on, browser input OFF: browser.input tools must not
        # be exposed even though --write is present.
        setup = tui.BridgeSetup(
            "chatgpt-web",
            9902,
            ("karox.repo.read_file", "karox.repo.write_file", "karox.browser.snapshot"),
            tunnel_provider="cloudflare",
        )
        launch = tui._managed_web_bridge_launch(Path.cwd(), setup)
        self.assertIn("--write", launch.argv)
        self.assertNotIn("karox.browser.open", launch.argv)
        self.assertNotIn("karox.browser.click", launch.argv)
        self.assertIn("karox.browser.snapshot", launch.argv)


if __name__ == "__main__":
    unittest.main()
