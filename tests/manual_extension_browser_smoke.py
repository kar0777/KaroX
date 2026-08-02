"""Manual real-Chrome smoke for the KaroX Manifest V3 browser backend."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy
from karox.extension_browser import ChromeExtensionBrowserSessionManager


class LiveChromeExtensionSmoke(unittest.TestCase):
    def test_real_extension_browser_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            session_id = "manual-extension-smoke"
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore(session_id),
                BrowserAccessPolicy(
                    session_id=session_id,
                    external_https=True,
                    headed=True,
                    user_takeover=True,
                    network_inspection=True,
                    backend="extension",
                ),
            )
            try:
                opened = manager.open(
                    {"url": "https://example.com/", "width": 1180, "height": 760},
                    40,
                )
                manager.wait_for({"selector": "h1", "state": "visible"}, 30)
                snapshot = manager.snapshot({}, 20)
                tabs = manager.tabs({}, 20)
                screenshot = manager.screenshot({"name": "extension-live"}, 20)
                takeover = manager.request_user_takeover(
                    {"reason": "live smoke"}, 10
                )
                resumed = manager.resume_after_user_takeover({}, 10)
                payload = {
                    "engine": opened.get("engine"),
                    "background": opened.get("background"),
                    "profile_persistent": opened.get("profile_persistent"),
                    "title": snapshot.get("title"),
                    "heading": (snapshot.get("snapshot") or {}).get("headings", [{}])[0].get("text"),
                    "tabs": tabs.get("count"),
                    "branding_tab_id": tabs.get("branding_tab_id"),
                    "screenshot_artifact_id": screenshot.get("artifact_id"),
                    "takeover_tab_id": takeover.get("tab_id"),
                    "resume_tab_id": resumed.get("tab_id"),
                }
                print("KAROX_EXTENSION_SMOKE=" + json.dumps(payload, sort_keys=True))
                self.assertEqual(payload["engine"], "chrome_extension_mv3")
                self.assertEqual(payload["title"], "Example Domain")
                self.assertEqual(payload["heading"], "Example Domain")
                self.assertTrue(payload["profile_persistent"])
                self.assertTrue(payload["branding_tab_id"])
                self.assertEqual(payload["takeover_tab_id"], payload["resume_tab_id"])
                self.assertTrue(payload["screenshot_artifact_id"])
            finally:
                manager.close(force=True)


if __name__ == "__main__":
    unittest.main()
