#!/usr/bin/env python3
"""Build a KaroX portable release bundle from already-verified inputs.

The builder is deliberately offline.  Release CI must provide:

* one KaroX wheel;
* one platform-appropriate, pinned uv binary;
* the expected SHA-256 of that uv binary;
* uv's MIT and Apache-2.0 license files.

This keeps network trust and artifact verification in the release workflow while
making bundle composition deterministic and testable.  The resulting launcher
uses the bundled uv to obtain a managed Python 3.12 runtime when the user first
starts KaroX; no system Python is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_WHEEL_RE = re.compile(r"^karox_runtime-(?P<version>[^-]+)-.+\.whl$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLATFORMS = {"windows-x64", "windows-arm64", "linux-x64", "linux-arm64", "macos-x64", "macos-arm64"}


class PortableBundleError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PortableBundleError(f"{label} does not exist: {resolved}")
    return resolved


def _version_from_wheel(wheel: Path) -> str:
    match = _WHEEL_RE.fullmatch(wheel.name)
    if match is None:
        raise PortableBundleError("wheel must be named karox_runtime-<version>-*.whl")
    return match.group("version")


def _copy_executable(source: Path, target: Path) -> None:
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_windows_cmd(path: Path) -> None:
    path.write_text(
        "@echo off\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0karox.ps1\" %*\r\n",
        encoding="ascii",
    )


def _archive_directory(bundle_dir: Path, output_dir: Path, platform: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if platform.startswith("windows-"):
        archive = output_dir / f"{bundle_dir.name}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(bundle_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(bundle_dir.name) / path.relative_to(bundle_dir))
        return archive

    archive = output_dir / f"{bundle_dir.name}.tar.gz"

    def portable_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
        # Preserve executable launchers even when a Unix archive is composed by
        # a Windows release runner, where chmod does not carry POSIX execute bits.
        if info.isfile() and Path(info.name).name in {"uv", "karox"}:
            info.mode = 0o755
        return info

    with tarfile.open(archive, "w:gz") as tf:
        tf.add(bundle_dir, arcname=bundle_dir.name, recursive=True, filter=portable_mode)
    return archive


def build_portable_bundle(
    *,
    wheel: Path,
    uv_binary: Path,
    expected_uv_sha256: str,
    uv_version: str,
    uv_license_mit: Path,
    uv_license_apache: Path,
    platform: str,
    output_dir: Path,
) -> Path:
    wheel = _required_file(wheel, "KaroX wheel")
    uv_binary = _required_file(uv_binary, "uv binary")
    uv_license_mit = _required_file(uv_license_mit, "uv MIT license")
    uv_license_apache = _required_file(uv_license_apache, "uv Apache license")
    if platform not in _PLATFORMS:
        raise PortableBundleError(f"unsupported portable platform: {platform}")
    if not isinstance(uv_version, str) or not uv_version.strip():
        raise PortableBundleError("uv version must not be empty")
    expected = str(expected_uv_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise PortableBundleError("expected uv SHA-256 must contain exactly 64 lowercase hex characters")
    actual_uv_sha256 = sha256_file(uv_binary)
    if actual_uv_sha256 != expected:
        raise PortableBundleError("uv binary SHA-256 does not match the pinned release value")

    version = _version_from_wheel(wheel)
    launcher_source = ROOT / "scripts" / (
        "portable_launch.ps1" if platform.startswith("windows-") else "portable_launch.sh"
    )
    launcher_source = _required_file(launcher_source, "portable launcher")
    karox_license = _required_file(ROOT / "LICENSE", "KaroX license")
    bundle_name = f"KaroX-v{version}-{platform}-portable"

    with tempfile.TemporaryDirectory(prefix="karox-portable-") as temporary:
        bundle_dir = Path(temporary) / bundle_name
        bundle_dir.mkdir(parents=True)
        shutil.copy2(wheel, bundle_dir / wheel.name)
        shutil.copy2(karox_license, bundle_dir / "LICENSE-KAROX.txt")
        shutil.copy2(uv_license_mit, bundle_dir / "LICENSE-UV-MIT.txt")
        shutil.copy2(uv_license_apache, bundle_dir / "LICENSE-UV-APACHE-2.0.txt")

        if platform.startswith("windows-"):
            shutil.copy2(uv_binary, bundle_dir / "uv.exe")
            shutil.copy2(launcher_source, bundle_dir / "karox.ps1")
            _write_windows_cmd(bundle_dir / "karox.cmd")
            launcher_name = "karox.cmd"
            uv_name = "uv.exe"
        else:
            _copy_executable(uv_binary, bundle_dir / "uv")
            _copy_executable(launcher_source, bundle_dir / "karox")
            launcher_name = "karox"
            uv_name = "uv"

        manifest = {
            "schema_version": 1,
            "product": "KaroX",
            "version": version,
            "platform": platform,
            "python_request": "3.12",
            "launcher": launcher_name,
            "wheel": {"name": wheel.name, "sha256": sha256_file(wheel)},
            "uv": {"name": uv_name, "version": uv_version.strip(), "sha256": actual_uv_sha256},
        }
        (bundle_dir / "PORTABLE-MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "THIRD-PARTY-NOTICES.txt").write_text(
            "This KaroX portable bundle redistributes the uv executable.\n"
            f"uv version: {uv_version.strip()}\n"
            "Project: https://github.com/astral-sh/uv\n"
            "License: dual MIT OR Apache-2.0; full texts are included beside this notice.\n",
            encoding="utf-8",
        )
        return _archive_directory(bundle_dir, output_dir.expanduser().resolve(), platform)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an offline-composed KaroX portable bundle.")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--uv-sha256", required=True)
    parser.add_argument("--uv-version", required=True)
    parser.add_argument("--uv-license-mit", type=Path, required=True)
    parser.add_argument("--uv-license-apache", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(_PLATFORMS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        archive = build_portable_bundle(
            wheel=args.wheel,
            uv_binary=args.uv,
            expected_uv_sha256=args.uv_sha256,
            uv_version=args.uv_version,
            uv_license_mit=args.uv_license_mit,
            uv_license_apache=args.uv_license_apache,
            platform=args.platform,
            output_dir=args.output_dir,
        )
    except PortableBundleError as exc:
        parser.error(str(exc))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
