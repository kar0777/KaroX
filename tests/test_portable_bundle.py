"""Tests for offline composition of KaroX portable release artifacts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from _support import ROOT


def _load_builder():
    path = ROOT / "scripts" / "build_portable_bundle.py"
    spec = importlib.util.spec_from_file_location("_karox_portable_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _fixture(root: Path) -> dict[str, object]:
    wheel = root / "karox_runtime-5.0.0-py3-none-any.whl"
    wheel.write_bytes(b"fake-wheel-for-bundle-contract")
    uv = root / "uv-input"
    uv.write_bytes(b"fake-pinned-uv-binary")
    mit = root / "LICENSE-MIT"
    apache = root / "LICENSE-APACHE"
    mit.write_text("fake MIT license fixture\n", encoding="utf-8")
    apache.write_text("fake Apache license fixture\n", encoding="utf-8")
    return {
        "wheel": wheel,
        "uv_binary": uv,
        "expected_uv_sha256": hashlib.sha256(uv.read_bytes()).hexdigest(),
        "uv_version": "0.12.1",
        "uv_license_mit": mit,
        "uv_license_apache": apache,
        "output_dir": root / "out",
    }


def test_windows_bundle_contains_verified_runtime_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        archive = builder.build_portable_bundle(platform="windows-x64", **args)
        assert archive.name == "KaroX-v5.0.0-windows-x64-portable.zip"
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            root = "KaroX-v5.0.0-windows-x64-portable/"
            for name in (
                "uv.exe",
                "karox.ps1",
                "karox.cmd",
                "karox_runtime-5.0.0-py3-none-any.whl",
                "PORTABLE-MANIFEST.json",
                "LICENSE-KAROX.txt",
                "LICENSE-UV-MIT.txt",
                "LICENSE-UV-APACHE-2.0.txt",
                "THIRD-PARTY-NOTICES.txt",
            ):
                assert root + name in names
            manifest = json.loads(zf.read(root + "PORTABLE-MANIFEST.json"))
        assert manifest["platform"] == "windows-x64"
        assert manifest["python_request"] == "3.12"
        assert manifest["uv"]["version"] == "0.12.1"
        assert manifest["uv"]["sha256"] == args["expected_uv_sha256"]


def test_linux_bundle_contains_executable_launcher_and_uv() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        archive = builder.build_portable_bundle(platform="linux-x64", **args)
        assert archive.name == "KaroX-v5.0.0-linux-x64-portable.tar.gz"
        with tarfile.open(archive, "r:gz") as tf:
            root = "KaroX-v5.0.0-linux-x64-portable/"
            uv = tf.getmember(root + "uv")
            launcher = tf.getmember(root + "karox")
            assert uv.mode & 0o111
            assert launcher.mode & 0o111
            manifest_file = tf.extractfile(root + "PORTABLE-MANIFEST.json")
            assert manifest_file is not None
            manifest = json.loads(manifest_file.read())
        assert manifest["platform"] == "linux-x64"
        assert manifest["launcher"] == "karox"


def test_wrong_uv_hash_fails_closed_before_bundle_creation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        args["expected_uv_sha256"] = "0" * 64
        with pytest.raises(builder.PortableBundleError, match="does not match"):
            builder.build_portable_bundle(platform="linux-x64", **args)
        assert not Path(args["output_dir"]).exists()


def test_invalid_uv_hash_format_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        args["expected_uv_sha256"] = "not-a-sha"
        with pytest.raises(builder.PortableBundleError, match="64 lowercase hex"):
            builder.build_portable_bundle(platform="macos-arm64", **args)


def test_missing_third_party_license_blocks_redistribution() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        Path(args["uv_license_mit"]).unlink()
        with pytest.raises(builder.PortableBundleError, match="MIT license"):
            builder.build_portable_bundle(platform="windows-x64", **args)


def test_unsupported_platform_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        args = _fixture(Path(directory))
        with pytest.raises(builder.PortableBundleError, match="unsupported"):
            builder.build_portable_bundle(platform="plan9-x64", **args)
