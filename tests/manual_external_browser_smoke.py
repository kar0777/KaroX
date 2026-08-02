"""Manual external-browser smoke test; intentionally excluded from normal collection."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import initialize_git_repository

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy, SecureBrowserSessionManager
from karox.browser_session import _validate_local_url
from karox.models import AccessProfile
from karox.web_bridge_launcher import WebBridgeConnectConfig, web_bridge_diagnostics


class ExternalBrowserSmoke(unittest.TestCase):
    def test_example_tabs_snapshot_screenshot_and_signup_form(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_runtime = os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
            repository = root / "repo"
            initialize_git_repository(repository)
            manager = SecureBrowserSessionManager(
                ArtifactStore("external-smoke"),
                BrowserAccessPolicy(
                    session_id="external-smoke",
                    localhost=True,
                    external_https=True,
                    headed=False,
                    user_takeover=False,
                    network_inspection=True,
                ),
            )
            try:
                opened = manager.open({"url": "https://example.com/"}, 45)
                first_tab = opened["tab_id"]
                snapshot = manager.snapshot({}, 20)
                screenshot = manager.screenshot(
                    {"full_page": True, "name": "example-smoke"}, 30
                )
                second = manager.new_tab(
                    {"url": "https://example.com/?karox_second_tab=1"}, 45
                )
                second_tab = second["tab_id"]
                manager.switch_tab({"tab_id": first_tab}, 10)
                manager.close_tab({"tab_id": second_tab}, 10)
                after_close = manager.tabs({}, 10)

                signup = manager.new_tab(
                    {"url": "https://gitlab.com/users/sign_up"}, 45
                )
                manager.wait_for({"selector": "input", "state": "attached"}, 20)
                signup_snapshot = manager.snapshot({}, 20)
                network = manager.network_requests(
                    {"url_contains": "gitlab", "fields": ["model", "usage", "credits", "plan", "trial", "subscription"]},
                    20,
                )
                # No fill/click/submit occurs. The page is inspected only to the
                # first form, then its tab is closed.
                manager.close_tab({"tab_id": signup["tab_id"]}, 10)

                config = WebBridgeConnectConfig(
                    profile="chatgpt-web",
                    repository=repository,
                    access_profile=AccessProfile.BROWSER_CONTROL,
                    tunnel="custom",
                    public_url="https://bridge.example",
                    browser_external_https=True,
                    browser_network_inspection=True,
                )
                diagnostics = web_bridge_diagnostics(
                    config, session_id="external-smoke"
                )
                legacy_local = _validate_local_url("http://127.0.0.1:8080/")

                result = {
                    "example_url": snapshot["url"],
                    "example_title": snapshot["title"],
                    "screenshot_artifact_id": screenshot["artifact_id"],
                    "screenshot_sha256": screenshot["sha256"],
                    "tabs_after_close": after_close["count"],
                    "active_tab_after_close": after_close["active_tab_id"],
                    "signup_url": signup_snapshot["url"],
                    "signup_title": signup_snapshot["title"],
                    "signup_input_count": len(
                        signup_snapshot.get("snapshot", {}).get("inputs", [])
                    ),
                    "redacted_network_request_count": network["count"],
                    "external_https": diagnostics["browser_permission"][
                        "external_https"
                    ],
                    "network_inspection": diagnostics["browser_permission"][
                        "network_inspection"
                    ],
                    "context_per_session": diagnostics["browser_isolation"][
                        "context_per_session"
                    ],
                    "legacy_localhost_url": legacy_local,
                }
                print("KAROX_EXTERNAL_BROWSER_SMOKE=" + json.dumps(result, sort_keys=True))
                self.assertIn("example.com", snapshot["url"])
                self.assertEqual(after_close["count"], 1)
                self.assertEqual(after_close["active_tab_id"], first_tab)
                self.assertIn("gitlab.com/users/sign_up", signup_snapshot["url"])
                self.assertGreater(
                    len(signup_snapshot.get("snapshot", {}).get("inputs", [])), 0
                )
                self.assertGreater(network["count"], 0)
                self.assertTrue(diagnostics["browser_permission"]["external_https"])
                self.assertEqual(legacy_local, "http://127.0.0.1:8080/")
            finally:
                manager.close()
                if old_runtime is None:
                    os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
                else:
                    os.environ["KAROX_VNEXT_RUNTIME_DIR"] = old_runtime


if __name__ == "__main__":
    unittest.main()
