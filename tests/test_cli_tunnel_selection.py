"""CLI auto selection resolves once; explicit/saved tunnel choices remain stable."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC
from karox import cli


class FinalCLIReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "KAROX_CONFIG_DIR": self.tmp.name,
                "KAROX_VNEXT_CONFIG_DIR": self.tmp.name,
                "KAROX_LEGACY_CONFIG_DIR": self.tmp.name,
                "KAROX_RUNTIME_DIR": self.tmp.name,
                "KAROX_VNEXT_RUNTIME_DIR": self.tmp.name,
                "XDG_CONFIG_HOME": self.tmp.name,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_cli_connect_auto_and_explicit(self):
        for present in (None, "/fake/tailscale"):
            for requested in (None, "auto", "tailscale", "cloudflare", "custom"):
                with (
                    self.subTest(present=present, requested=requested),
                    patch("karox.tunnel_bootstrap.find_tailscale", return_value=present),
                ):
                    argv = ["bridge", "connect", "chatgpt-web", "--repository", self.tmp.name]
                    if requested:
                        argv += ["--tunnel", requested]
                    if requested == "custom":
                        argv += ["--public-url", "https://example.com"]
                    args = cli._parser().parse_args(argv)
                    cfg = cli._direct_connect_config(args)
                    self.assertEqual(
                        cfg.tunnel,
                        requested
                        if requested not in (None, "auto")
                        else ("tailscale" if present else "cloudflare"),
                    )

    def saved(self, argv):
        return cli._handle_bridge_saved(cli._parser().parse_args(["bridge", "saved"] + argv))

    def store(self):
        from karox.web_bridge_profiles import WebBridgeProfileStore

        return WebBridgeProfileStore(Path(self.tmp.name) / "profiles.json")

    def test_saved_default_auto_persistence(self):
        store = self.store()
        with (
            patch.object(cli, "WebBridgeProfileStore", return_value=store),
            patch.object(cli, "_emit"),
        ):
            for i, present in enumerate((None, "/fake/tailscale")):
                for requested in (None, "auto"):
                    with (
                        self.subTest(present=present, requested=requested),
                        patch("karox.tunnel_bootstrap.find_tailscale", return_value=present),
                    ):
                        name = "auto" + str(i) + str(requested)
                        argv = [
                            "create",
                            name,
                            "--repository",
                            self.tmp.name,
                            "--target-profile",
                            "claude-web",
                        ]
                        if requested:
                            argv += ["--tunnel", requested]
                        self.saved(argv)
                        self.assertEqual(
                            self.store().get(name).tunnel, "tailscale" if present else "cloudflare"
                        )
                        self.assertEqual(self.store().get(name).target_profile, "claude-web")

    def test_saved_explicit_creation_and_edit_persistence(self):
        store = self.store()
        with (
            patch.object(cli, "WebBridgeProfileStore", return_value=store),
            patch.object(cli, "_emit"),
            patch.object(cli, "saved_web_bridge_identity_exists", return_value=False),
        ):
            for i, present in enumerate((None, "/fake/tailscale")):
                for requested in ("tailscale", "cloudflare", "custom"):
                    with (
                        self.subTest(present=present, requested=requested),
                        patch("karox.tunnel_bootstrap.find_tailscale", return_value=present),
                    ):
                        name = "explicit" + str(i) + requested
                        argv = [
                            "create",
                            name,
                            "--repository",
                            self.tmp.name,
                            "--target-profile",
                            "claude-web",
                            "--tunnel",
                            requested,
                        ]
                        if requested == "custom":
                            argv += ["--public-url", "https://example.com"]
                        self.saved(argv)
                        self.assertEqual(self.store().get(name).tunnel, requested)
                        self.assertEqual(self.store().get(name).target_profile, "claude-web")
                        self.saved(["edit", name, "--language", "ru"])
                        self.assertEqual(self.store().get(name).tunnel, requested)
                        if requested == "custom":
                            self.assertEqual(
                                self.store().get(name).public_url, "https://example.com"
                            )
                        for edited in ("tailscale", "cloudflare", "custom", "auto"):
                            argv = ["edit", name, "--tunnel", edited]
                            if edited == "custom":
                                argv += ["--public-url", "https://example.com"]
                            self.saved(argv)
                            expected = (
                                edited
                                if edited != "auto"
                                else ("tailscale" if present else "cloudflare")
                            )
                            self.assertEqual(self.store().get(name).tunnel, expected)
                            self.assertEqual(self.store().get(name).target_profile, "claude-web")
                        before = self.store().get(name).tunnel
                        with patch(
                            "karox.tunnel_bootstrap.find_tailscale", return_value=not present
                        ):
                            self.saved(["edit", name, "--language", "en"])
                        self.assertEqual(self.store().get(name).tunnel, before)

    def test_top_level_connect(self):
        for present in (None, "/fake/tailscale"):
            for requested in (None, "auto", "tailscale", "cloudflare", "custom"):
                with (
                    self.subTest(present=present, requested=requested),
                    patch("karox.tunnel_bootstrap.find_tailscale", return_value=present),
                    patch.object(cli, "run_web_bridge", return_value=0) as launch,
                ):
                    argv = ["connect", "chatgpt", "--repository", self.tmp.name]
                    if requested:
                        argv += ["--tunnel", requested]
                    if requested == "custom":
                        argv += ["--public-url", "https://example.com"]
                    self.assertEqual(cli._handle_connect(cli._parser().parse_args(argv)), 0)
                    self.assertEqual(
                        launch.call_args.args[0].tunnel,
                        requested
                        if requested not in (None, "auto")
                        else ("tailscale" if present else "cloudflare"),
                    )
