#!/usr/bin/env python3
"""Validate and expose the pinned uv inputs used by KaroX portable releases."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_PINS: Final[Path] = ROOT / "scripts" / "portable_uv_pins.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_EXPECTED: Final[dict[str, tuple[str, str]]] = {
    "macos-arm64": ("uv-aarch64-apple-darwin.tar.gz", "uv"),
    "macos-x64": ("uv-x86_64-apple-darwin.tar.gz", "uv"),
    "windows-arm64": ("uv-aarch64-pc-windows-msvc.zip", "uv.exe"),
    "windows-x64": ("uv-x86_64-pc-windows-msvc.zip", "uv.exe"),
    "linux-arm64": ("uv-aarch64-unknown-linux-gnu.tar.gz", "uv"),
    "linux-x64": ("uv-x86_64-unknown-linux-gnu.tar.gz", "uv"),
}


class PortableUvPinError(ValueError):
    pass


@dataclass(frozen=True)
class PortableUvPin:
    platform: str
    archive: str
    url: str
    sha256: str
    binary: str


@dataclass(frozen=True)
class PortableUvPins:
    version: str
    project: str
    release_url: str
    license_mit_url: str
    license_apache_url: str
    platforms: tuple[PortableUvPin, ...]


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortableUvPinError(f"{field} must be a non-empty string")
    return value.strip()


def load_portable_uv_pins(path: Path = DEFAULT_PINS) -> PortableUvPins:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortableUvPinError(f"cannot read portable uv pins: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise PortableUvPinError("portable uv pins have an unsupported schema")
    if set(raw) != {
        "schema_version",
        "project",
        "version",
        "release_url",
        "license_mit_url",
        "license_apache_url",
        "platforms",
    }:
        raise PortableUvPinError("portable uv pin manifest has unexpected or missing fields")

    project = _required_string(raw.get("project"), "project")
    if project != "astral-sh/uv":
        raise PortableUvPinError("portable runtime may only pin the official astral-sh/uv project")
    version = _required_string(raw.get("version"), "version")
    if _VERSION.fullmatch(version) is None:
        raise PortableUvPinError("uv version must be a stable numeric release")

    release_url = _required_string(raw.get("release_url"), "release_url")
    expected_release = f"https://github.com/astral-sh/uv/releases/tag/{version}"
    if release_url != expected_release:
        raise PortableUvPinError("uv release URL must match the pinned official tag")
    license_mit_url = _required_string(raw.get("license_mit_url"), "license_mit_url")
    license_apache_url = _required_string(raw.get("license_apache_url"), "license_apache_url")
    if license_mit_url != f"https://raw.githubusercontent.com/astral-sh/uv/{version}/LICENSE-MIT":
        raise PortableUvPinError("uv MIT license URL must be pinned to the same tag")
    if license_apache_url != f"https://raw.githubusercontent.com/astral-sh/uv/{version}/LICENSE-APACHE":
        raise PortableUvPinError("uv Apache license URL must be pinned to the same tag")

    platforms_raw = raw.get("platforms")
    if not isinstance(platforms_raw, dict) or set(platforms_raw) != set(_EXPECTED):
        raise PortableUvPinError("portable uv pins must cover exactly the six supported platforms")
    pins: list[PortableUvPin] = []
    for platform in sorted(_EXPECTED):
        value = platforms_raw.get(platform)
        if not isinstance(value, dict) or set(value) != {"archive", "url", "sha256", "binary"}:
            raise PortableUvPinError(f"invalid uv pin object for {platform}")
        archive = _required_string(value.get("archive"), f"{platform}.archive")
        binary = _required_string(value.get("binary"), f"{platform}.binary")
        expected_archive, expected_binary = _EXPECTED[platform]
        if archive != expected_archive or binary != expected_binary:
            raise PortableUvPinError(f"unexpected uv artifact mapping for {platform}")
        url = _required_string(value.get("url"), f"{platform}.url")
        expected_url = f"https://releases.astral.sh/github/uv/releases/download/{version}/{archive}"
        if url != expected_url:
            raise PortableUvPinError(f"uv artifact URL is not the pinned official release for {platform}")
        sha256 = _required_string(value.get("sha256"), f"{platform}.sha256")
        if _SHA256.fullmatch(sha256) is None:
            raise PortableUvPinError(f"invalid SHA-256 for {platform}")
        pins.append(PortableUvPin(platform, archive, url, sha256, binary))

    return PortableUvPins(
        version=version,
        project=project,
        release_url=release_url,
        license_mit_url=license_mit_url,
        license_apache_url=license_apache_url,
        platforms=tuple(pins),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tsv", action="store_true")
    group.add_argument("--version", action="store_true")
    group.add_argument("--license-mit", action="store_true")
    group.add_argument("--license-apache", action="store_true")
    args = parser.parse_args(argv)
    pins = load_portable_uv_pins(args.pins)
    if args.version:
        print(pins.version)
    elif args.license_mit:
        print(pins.license_mit_url)
    elif args.license_apache:
        print(pins.license_apache_url)
    else:
        for pin in pins.platforms:
            print("\t".join((pin.platform, pin.archive, pin.url, pin.sha256, pin.binary)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
