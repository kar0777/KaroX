"""Dev/Dogfood/Stable channel isolation of the state namespace.

A Dogfood install must be evidence about the packaged build, which it
cannot be if it shares sessions, credentials, or map state with the
Stable (or source-dev) install. The channel comes from KAROX_CHANNEL;
unknown spellings fall back to stable rather than inventing a directory
tree nothing else knows about. Explicit KAROX_*_DIR overrides always win.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401
from karox import paths


class ChannelResolutionTests(unittest.TestCase):
    def test_default_channel_is_stable(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAROX_CHANNEL", None)
            self.assertEqual(paths.channel(), "stable")

    def test_known_channels_resolve_case_insensitively(self) -> None:
        for raw, expected in (
            ("dogfood", "dogfood"),
            ("DogFood", "dogfood"),
            ("dev", "dev"),
            ("STABLE", "stable"),
        ):
            with patch.dict(os.environ, {"KAROX_CHANNEL": raw}):
                self.assertEqual(paths.channel(), expected)

    def test_unknown_channel_falls_back_to_stable(self) -> None:
        with patch.dict(os.environ, {"KAROX_CHANNEL": "canary"}):
            self.assertEqual(paths.channel(), "stable")


class NamespaceIsolationTests(unittest.TestCase):
    """Directory names per channel, tested at a temporary base.

    The repository test harness redirects the real user locations, and the
    KAROX_VNEXT_* overrides win before the channel is consulted, so these
    tests clear the overrides and re-base APPDATA/LOCALAPPDATA/XDG_* onto a
    temporary directory -- the pattern paths.py itself documents as the
    legitimate way to test resolution.
    """

    def _channel_env(self, channel: str, base: Path) -> dict[str, str]:
        return {
            "KAROX_CHANNEL": channel,
            "KAROX_VNEXT_CONFIG_DIR": "",
            "KAROX_CONFIG_DIR": "",
            "KAROX_VNEXT_RUNTIME_DIR": "",
            "KAROX_RUNTIME_DIR": "",
            "APPDATA": str(base / "roaming"),
            "LOCALAPPDATA": str(base / "local"),
            "XDG_CONFIG_HOME": str(base / "xdg-config"),
            "XDG_DATA_HOME": str(base / "xdg-data"),
        }

    def test_channel_app_name_maps_every_channel(self) -> None:
        expected = {
            "stable": paths.APP_NAME,
            "dogfood": f"{paths.APP_NAME}-dogfood",
            "dev": f"{paths.APP_NAME}-dev",
        }
        for name, directory in expected.items():
            with patch.dict(os.environ, {"KAROX_CHANNEL": name}):
                self.assertEqual(paths._channel_app_name(), directory)

    def test_channels_produce_disjoint_config_and_runtime_dirs(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            seen: set[Path] = set()
            for name in paths.CHANNELS:
                with patch.dict(os.environ, self._channel_env(name, base)):
                    config = paths.config_dir()
                    runtime = paths.runtime_dir()
                self.assertNotIn(config, seen)
                seen.add(config)
                self.assertNotIn(runtime, seen)
                seen.add(runtime)

    def test_stable_and_dogfood_directory_names(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            with patch.dict(os.environ, self._channel_env("stable", base)):
                self.assertEqual(paths.config_dir().name, paths.APP_NAME)
                self.assertEqual(paths.runtime_dir().name, paths.APP_NAME)
            with patch.dict(os.environ, self._channel_env("dogfood", base)):
                self.assertEqual(paths.config_dir().name, "KaroX-dogfood")
                self.assertEqual(paths.runtime_dir().name, "KaroX-dogfood")
                self.assertIn("KaroX-dogfood", paths.session_dir().parts)

    def test_explicit_override_beats_the_channel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = self._channel_env("dogfood", base)
            env["KAROX_VNEXT_RUNTIME_DIR"] = str(base / "kx-override")
            with patch.dict(os.environ, env):
                self.assertEqual(
                    paths.runtime_dir(), (base / "kx-override").resolve()
                )


if __name__ == "__main__":
    unittest.main()
