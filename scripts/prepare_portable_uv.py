#!/usr/bin/env python3
"""Prepare one pinned uv release artifact for KaroX portable bundle CI.

The input URL and archive SHA-256 come exclusively from portable_uv_pins.json.
The archive is streamed to disk while hashing, rejected on mismatch, and only the
expected uv executable is extracted.  The original verified archive is retained
so release CI can run GitHub Artifact Attestation verification before bundle
composition.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO, Final

from portable_uv_pins import DEFAULT_PINS, PortableUvPin, load_portable_uv_pins

_MAX_ARCHIVE_BYTES: Final[int] = 128 * 1024 * 1024
_MAX_BINARY_BYTES: Final[int] = 128 * 1024 * 1024
_MAX_LICENSE_BYTES: Final[int] = 256 * 1024
_USER_AGENT: Final[str] = "KaroX-release-builder/5"


class PortableUvPrepareError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_download(url: str, target: Path, *, max_bytes: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PortableUvPrepareError("downloaded release artifact exceeds the size limit")
                digest.update(chunk)
                out.write(chunk)
    except PortableUvPrepareError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise PortableUvPrepareError("could not download pinned uv release input") from exc
    return digest.hexdigest()


def _safe_member_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = Path(normalized)
    first = normalized.split("/", 1)[0]
    has_drive_prefix = len(first) >= 2 and first[1] == ":"
    return (
        bool(normalized)
        and not normalized.startswith("/")
        and not has_drive_prefix
        and not path.is_absolute()
        and ".." not in path.parts
    )


def _copy_limited(source: BinaryIO, target: Path, *, max_bytes: int) -> None:
    total = 0
    with target.open("wb") as out:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                target.unlink(missing_ok=True)
                raise PortableUvPrepareError("uv executable exceeds the size limit")
            out.write(chunk)


def extract_uv_binary(archive: Path, pin: PortableUvPin, output: Path) -> Path:
    """Extract only the expected executable, rejecting links and path tricks."""

    candidates: list[tuple[str, object]] = []
    if pin.archive.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if not _safe_member_name(info.filename):
                        raise PortableUvPrepareError("uv zip contains an unsafe member path")
                    if not info.is_dir() and Path(info.filename).name == pin.binary:
                        candidates.append((info.filename, info))
                if len(candidates) != 1:
                    raise PortableUvPrepareError("uv archive must contain exactly one expected executable")
                _, info_obj = candidates[0]
                assert isinstance(info_obj, zipfile.ZipInfo)
                if info_obj.file_size > _MAX_BINARY_BYTES:
                    raise PortableUvPrepareError("uv executable exceeds the size limit")
                with zf.open(info_obj) as source:
                    _copy_limited(source, output, max_bytes=_MAX_BINARY_BYTES)
        except PortableUvPrepareError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise PortableUvPrepareError("uv zip archive is invalid") from exc
    elif pin.archive.endswith(".tar.gz"):
        try:
            with tarfile.open(archive, "r:gz") as tf:
                members = tf.getmembers()
                for member in members:
                    if not _safe_member_name(member.name):
                        raise PortableUvPrepareError("uv tar archive contains an unsafe member path")
                    if member.issym() or member.islnk():
                        if Path(member.name).name == pin.binary:
                            raise PortableUvPrepareError("uv executable may not be a symlink or hardlink")
                        continue
                    if member.isfile() and Path(member.name).name == pin.binary:
                        candidates.append((member.name, member))
                if len(candidates) != 1:
                    raise PortableUvPrepareError("uv archive must contain exactly one expected executable")
                _, member_obj = candidates[0]
                assert isinstance(member_obj, tarfile.TarInfo)
                if member_obj.size > _MAX_BINARY_BYTES:
                    raise PortableUvPrepareError("uv executable exceeds the size limit")
                source = tf.extractfile(member_obj)
                if source is None:
                    raise PortableUvPrepareError("uv executable could not be read from tar archive")
                with source:
                    _copy_limited(source, output, max_bytes=_MAX_BINARY_BYTES)
        except PortableUvPrepareError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise PortableUvPrepareError("uv tar archive is invalid") from exc
    else:
        raise PortableUvPrepareError("unsupported uv archive format")

    if not output.is_file():
        raise PortableUvPrepareError("uv executable was not extracted")
    if os.name != "nt":
        output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output


def _download_text(url: str, target: Path) -> None:
    digest = _stream_download(url, target, max_bytes=_MAX_LICENSE_BYTES)
    if not digest or not target.read_text(encoding="utf-8").strip():
        target.unlink(missing_ok=True)
        raise PortableUvPrepareError("downloaded uv license is empty or invalid")


def prepare_portable_uv(*, platform: str, output_dir: Path, pins_path: Path = DEFAULT_PINS) -> dict[str, str]:
    pins = load_portable_uv_pins(pins_path)
    pin = next((item for item in pins.platforms if item.platform == platform), None)
    if pin is None:
        raise PortableUvPrepareError(f"unsupported pinned portable platform: {platform}")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / pin.archive
    actual = _stream_download(pin.url, archive, max_bytes=_MAX_ARCHIVE_BYTES)
    if actual != pin.sha256:
        archive.unlink(missing_ok=True)
        raise PortableUvPrepareError("downloaded uv archive SHA-256 does not match the checked-in pin")

    binary = output_dir / pin.binary
    binary.unlink(missing_ok=True)
    extract_uv_binary(archive, pin, binary)
    license_mit = output_dir / "LICENSE-UV-MIT.txt"
    license_apache = output_dir / "LICENSE-UV-APACHE-2.0.txt"
    _download_text(pins.license_mit_url, license_mit)
    _download_text(pins.license_apache_url, license_apache)
    return {
        "platform": platform,
        "version": pins.version,
        "archive": str(archive),
        "archive_sha256": actual,
        "uv_binary": str(binary),
        "uv_binary_sha256": sha256_file(binary),
        "license_mit": str(license_mit),
        "license_apache": str(license_apache),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--format", choices=("json", "tsv"), default="json")
    args = parser.parse_args(argv)
    try:
        result = prepare_portable_uv(platform=args.platform, output_dir=args.output_dir, pins_path=args.pins)
    except PortableUvPrepareError as exc:
        parser.error(str(exc))
    if args.format == "tsv":
        print("\t".join(str(result[key]) for key in (
            "platform", "version", "archive", "uv_binary", "uv_binary_sha256", "license_mit", "license_apache"
        )))
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
