"""Regression tests for the safe child-environment allowlist.

``child_process_environment`` is the only seam between a hosted verification
command (``checks.run``) and the OS environment it runs in.  The allowlist is a
deny-by-default filter: only a curated set of benign names is forwarded, so a
secret the host happens to set never reaches a child.

This module pins two things the KaroX v5 acceptance run exposed:

1. ``PLAYWRIGHT_BROWSERS_PATH`` must be forwarded.  It is a *path-only*
   directory pointer a Playwright-based verification command (e.g.
   ``npm run test:smoke``) uses to locate its own already-downloaded browser
   binaries.  Without it the child falls back to the default per-user cache,
   misses the browser, and reports ``Executable doesn't exist`` even though the
   same command passes in a direct shell -- which is exactly the regression the
   acceptance run reproduced (smoke test passes directly, fails through KaroX).
2. ``NODE_OPTIONS`` must NOT be forwarded.  It can inject an arbitrary module
   via ``--require``/``--import`` and so is a code-injection vector a hosted
   allowlist must not hand to a child, regardless of how convenient it is.
   ``PLAYWRIGHT_DOWNLOAD_HOST`` is similarly a download-source redirect and is
   kept out for the same reason.
"""

from __future__ import annotations

import os
import unittest
from typing import Mapping

from _support import (  # noqa: F401 - inserts src on sys.path
    SRC,
    _CONFIG_OVERRIDES,
    _LEGACY_OVERRIDES,
    _RUNTIME_OVERRIDES,
    child_environment,
)

from karox.security import child_process_environment, _SAFE_CHILD_ENVIRONMENT


class SafeChildEnvironmentAllowlistTests(unittest.TestCase):
    def _env(self, **extra: str) -> Mapping[str, str]:
        merged = {"PATH": "/usr/bin", "USERPROFILE": "C:\\Users\\x", "LOCALAPPDATA": "C:\\x"}
        merged.update(extra)
        return child_process_environment(source=merged)

    def test_playwright_browsers_path_is_forwarded(self) -> None:
        # The acceptance regression: a smoke test run through KaroX must see the
        # same browser-cache directory a direct shell does.
        env = self._env(PLAYWRIGHT_BROWSERS_PATH=r"D:\DeveloperData\ms-playwright")
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], r"D:\DeveloperData\ms-playwright")

    def test_node_options_is_not_forwarded(self) -> None:
        # Code-injection vector (--require / --import): must never reach a child.
        env = self._env(NODE_OPTIONS="--require ./evil.mjs")
        self.assertNotIn("NODE_OPTIONS", env)

    def test_playwright_download_host_is_not_forwarded(self) -> None:
        # Download-source redirect: a hosted child must not be pointed at an
        # attacker-controlled browser-binary host.
        env = self._env(PLAYWRIGHT_DOWNLOAD_HOST="https://evil.example/playwright")
        self.assertNotIn("PLAYWRIGHT_DOWNLOAD_HOST", env)

    def test_arbitrary_secret_env_is_not_forwarded(self) -> None:
        # Deny-by-default: an unknown name (especially a credential-shaped one)
        # never reaches the child even if the host sets it.
        env = self._env(API_KEY="sk-supersecret", DATABASE_URL="postgres://u:p@h/db")
        self.assertNotIn("API_KEY", env)
        self.assertNotIn("DATABASE_URL", env)

    def test_baseline_benign_names_still_forwarded(self) -> None:
        # Adding the browser-cache path must not have loosened the existing
        # benign set: PATH / USERPROFILE / LOCALAPPDATA still come through.
        env = self._env()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["USERPROFILE"], "C:\\Users\\x")
        self.assertEqual(env["LOCALAPPDATA"], "C:\\x")

    def test_child_encoding_is_forced_to_utf8(self) -> None:
        # KaroX decodes captured output as UTF-8. A Python child that inherits a
        # non-UTF-8 console code page -- cp866 on a Russian-locale Windows install
        # -- writes its diagnostics in that code page instead, so the failing line
        # the agent has to act on arrived as bytes the decoder could not read and
        # ``errors="ignore"`` deleted them outright. The child's side of the
        # contract is therefore set here rather than hoped for.
        env = self._env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_forced_encoding_overrides_a_hostile_host_value(self) -> None:
        # Forwarding would put the host back in charge of an invariant the
        # runtime depends on, so the host's value loses.
        env = self._env(PYTHONIOENCODING="cp866", PYTHONUTF8="0")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_allowlist_is_lowercase_insensitive_and_credential_free(self) -> None:
        # The filter compares ``key.upper()``, so a mixed-case variant of a
        # safe name is still allowed; and no member of the allowlist is itself
        # credential-shaped (none match the secret-value patterns redact uses).
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", _SAFE_CHILD_ENVIRONMENT)
        for name in _SAFE_CHILD_ENVIRONMENT:
            self.assertEqual(name, name.upper())
            self.assertNotIn("KEY", name)
            self.assertNotIn("SECRET", name)
            self.assertNotIn("TOKEN", name)
            self.assertNotIn("PASSWORD", name)


if __name__ == "__main__":
    unittest.main()
