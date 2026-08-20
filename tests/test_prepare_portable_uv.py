"""Security tests for preparing pinned uv binaries in release CI."""
from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from _support import ROOT


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pins = _load("portable_uv_pins", ROOT / "scripts" / "portable_uv_pins.py")
prepare = _load("_prepare_portable_uv", ROOT / "scripts" / "prepare_portable_uv.py")


def _pin(platform: str):
    return next(item for item in pins.load_portable_uv_pins().platforms if item.platform == platform)


def test_extract_windows_zip_selects_only_expected_uv_binary() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("uv-x86_64/uv.exe", b"verified-uv")
            zf.writestr("uv-x86_64/uvx.exe", b"not-selected")
        output = root / "uv.exe"
        prepare.extract_uv_binary(archive, _pin("windows-x64"), output)
        assert output.read_bytes() == b"verified-uv"


def test_extract_linux_tar_selects_expected_regular_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            payload = b"verified-uv"
            info = tarfile.TarInfo("uv-x86_64/uv")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        output = root / "uv"
        prepare.extract_uv_binary(archive, _pin("linux-x64"), output)
        assert output.read_bytes() == b"verified-uv"


def test_zip_path_traversal_is_rejected_before_extraction() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../uv.exe", b"bad")
        with pytest.raises(prepare.PortableUvPrepareError, match="unsafe member path"):
            prepare.extract_uv_binary(archive, _pin("windows-x64"), root / "uv.exe")


def test_windows_drive_path_is_rejected_even_on_non_windows_builder() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("C:/escape/uv.exe", b"bad")
        with pytest.raises(prepare.PortableUvPrepareError, match="unsafe member path"):
            prepare.extract_uv_binary(archive, _pin("windows-x64"), root / "uv.exe")


def test_tar_symlink_named_uv_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("bundle/uv")
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/foreign"
            tf.addfile(info)
        with pytest.raises(prepare.PortableUvPrepareError, match="symlink or hardlink"):
            prepare.extract_uv_binary(archive, _pin("linux-x64"), root / "uv")


def test_duplicate_expected_binary_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "uv.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("one/uv.exe", b"one")
            zf.writestr("two/uv.exe", b"two")
        with pytest.raises(prepare.PortableUvPrepareError, match="exactly one"):
            prepare.extract_uv_binary(archive, _pin("windows-x64"), root / "uv.exe")


def test_cli_tsv_format_is_stable_for_release_workflow(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        prepare,
        "prepare_portable_uv",
        lambda **kwargs: {
            "platform": "linux-x64",
            "version": "0.12.3",
            "archive": "/tmp/uv.tar.gz",
            "archive_sha256": "a" * 64,
            "uv_binary": "/tmp/uv",
            "uv_binary_sha256": "b" * 64,
            "license_mit": "/tmp/LICENSE-MIT",
            "license_apache": "/tmp/LICENSE-APACHE",
        },
    )
    assert prepare.main(["--platform", "linux-x64", "--output-dir", ".", "--format", "tsv"]) == 0
    assert capsys.readouterr().out.strip().split("\t") == [
        "linux-x64",
        "0.12.3",
        "/tmp/uv.tar.gz",
        "/tmp/uv",
        "b" * 64,
        "/tmp/LICENSE-MIT",
        "/tmp/LICENSE-APACHE",
    ]


def test_prepare_deletes_archive_when_downloaded_hash_mismatches(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        def fake_download(url: str, target: Path, *, max_bytes: int) -> str:
            target.write_bytes(b"tampered")
            return "0" * 64
        monkeypatch.setattr(prepare, "_stream_download", fake_download)
        with pytest.raises(prepare.PortableUvPrepareError, match="SHA-256"):
            prepare.prepare_portable_uv(platform="linux-x64", output_dir=output)
        assert not (output / _pin("linux-x64").archive).exists()
