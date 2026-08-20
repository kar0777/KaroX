"""Contracts for the checked-in uv supply-chain pin manifest."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

from _support import ROOT


def _load_module():
    path = ROOT / "scripts" / "portable_uv_pins.py"
    spec = importlib.util.spec_from_file_location("_portable_uv_pins", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pins_module = _load_module()


def test_checked_in_manifest_is_valid_and_complete() -> None:
    pins = pins_module.load_portable_uv_pins()
    assert pins.project == "astral-sh/uv"
    assert pins.version == "0.12.3"
    assert {pin.platform for pin in pins.platforms} == {"windows-x64", "windows-arm64", "linux-x64", "linux-arm64", "macos-x64", "macos-arm64"}
    for pin in pins.platforms:
        assert len(pin.sha256) == 64
        assert pin.url.startswith("https://releases.astral.sh/github/uv/releases/download/0.12.3/")


def _mutated_manifest(mutator) -> Path:
    data = json.loads((ROOT / "scripts" / "portable_uv_pins.json").read_text(encoding="utf-8"))
    mutator(data)
    directory = tempfile.mkdtemp(prefix="karox-pin-test-")
    path = Path(directory) / "pins.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_different_project_is_rejected() -> None:
    path = _mutated_manifest(lambda data: data.__setitem__("project", "other/uv"))
    with pytest.raises(pins_module.PortableUvPinError, match="official"):
        pins_module.load_portable_uv_pins(path)


def test_url_must_match_same_pinned_tag_and_official_host() -> None:
    def mutate(data): data["platforms"]["windows-x64"]["url"] = "https://example.invalid/uv.zip"
    with pytest.raises(pins_module.PortableUvPinError, match="official release"):
        pins_module.load_portable_uv_pins(_mutated_manifest(mutate))


def test_missing_platform_is_rejected() -> None:
    def mutate(data): data["platforms"].pop("macos-arm64")
    with pytest.raises(pins_module.PortableUvPinError, match="six supported"):
        pins_module.load_portable_uv_pins(_mutated_manifest(mutate))


def test_bad_checksum_is_rejected() -> None:
    def mutate(data): data["platforms"]["linux-x64"]["sha256"] = "bad"
    with pytest.raises(pins_module.PortableUvPinError, match="SHA-256"):
        pins_module.load_portable_uv_pins(_mutated_manifest(mutate))


def test_license_urls_must_be_pinned_to_same_tag() -> None:
    path = _mutated_manifest(lambda data: data.__setitem__("license_mit_url", "https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-MIT"))
    with pytest.raises(pins_module.PortableUvPinError, match="same tag"):
        pins_module.load_portable_uv_pins(path)
