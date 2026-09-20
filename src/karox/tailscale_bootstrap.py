"""Checksum-pinned official Tailscale installer bootstrap (consent-required).

The first-run wizard may offer Tailscale installation. Nothing is downloaded
or executed without an explicit user consent action, and every download is
verified against SHA-256 digests pinned to the official
https://pkgs.tailscale.com/stable/ server. KaroX never signs into Tailscale
and never stores Tailscale credentials; the guided login stays with the user.
"""
from __future__ import annotations

import hashlib
import os
import platform
import stat
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .paths import runtime_dir

TAILSCALE_VERSION = "1.102.4"
PACKAGES_BASE = "https://pkgs.tailscale.com/stable/"
MAX_DOWNLOAD_BYTES = 96 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0
IO_TIMEOUT_SECONDS = 15.0
_CHUNK = 64 * 1024

TARMembers = ("tailscale/tailscale", "tailscale/tailscaled")


class TailscaleBootstrapError(RuntimeError):
    """A Tailscale installer could not be obtained and verified safely."""


@dataclass(frozen=True)
class TailscaleAsset:
    name: str
    sha256: str
    kind: str  # "exe" (Windows), "pkg" (macOS), "tgz" (Linux)


ASSETS: dict[tuple[str, str], TailscaleAsset] = {
    ("windows", "amd64"): TailscaleAsset(
        "tailscale-setup-full-1.102.4.exe",
        "6803a1f2b5ccb42b483c77cf3724ad78b2ac112b53e2d26b72bfc42726a54412",
        "exe",
    ),
    # The full installer embeds every MSI package, so one asset covers all
    # Windows architectures; MSI selection happens inside the installer.
    ("windows", "arm64"): TailscaleAsset(
        "tailscale-setup-full-1.102.4.exe",
        "6803a1f2b5ccb42b483c77cf3724ad78b2ac112b53e2d26b72bfc42726a54412",
        "exe",
    ),
    ("darwin", "amd64"): TailscaleAsset(
        "Tailscale-1.102.4-macos.pkg",
        "b40b733af76233fd1e4af7acaeb325268e55e6818c15c6e9aa9e78f427245c5b",
        "pkg",
    ),
    ("darwin", "arm64"): TailscaleAsset(
        "Tailscale-1.102.4-macos.pkg",
        "b40b733af76233fd1e4af7acaeb325268e55e6818c15c6e9aa9e78f427245c5b",
        "pkg",
    ),
    ("linux", "amd64"): TailscaleAsset(
        "tailscale_1.102.4_amd64.tgz",
        "50748df1045e60b5b695f19f4c56b0da36c019948b440fb456b6584a50f0d8b9",
        "tgz",
    ),
    ("linux", "arm64"): TailscaleAsset(
        "tailscale_1.102.4_arm64.tgz",
        "9dd1e6a592a014bbaea0103167ffe299adeda4ba14e078ce9c2895364f6c4c3f",
        "tgz",
    ),
}


def tailscale_asset() -> TailscaleAsset:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        # A 32-bit Python on a 64-bit Windows host must select the host asset.
        machine = os.environ.get("PROCESSOR_ARCHITEW6432", machine).lower()
    arch = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    asset = ASSETS.get((system, arch))
    if asset is None:
        raise TailscaleBootstrapError(
            f"No checksum-pinned Tailscale download for {system}/{arch}. "
            "Install Tailscale manually from https://tailscale.com/download and retry."
        )
    return asset


def _trusted_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.fragment
        ):
            return False
        return parts.hostname == "pkgs.tailscale.com" and parts.path.startswith("/stable/")
    except ValueError:
        return False


class _TrustedRedirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if time.monotonic() >= self.deadline:
            raise TailscaleBootstrapError("Tailscale download exceeded its time budget during redirects")
        if not _trusted_url(newurl):
            raise TailscaleBootstrapError("Tailscale download redirected outside trusted HTTPS sources")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(asset: TailscaleAsset, target: IO[bytes]) -> None:
    url = PACKAGES_BASE + asset.name
    if not _trusted_url(url):
        raise TailscaleBootstrapError("Tailscale download source is not trusted HTTPS")
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    request = Request(url, headers={"User-Agent": "KaroX-tailscale-bootstrap", "Accept-Encoding": "identity"})
    try:
        with build_opener(_TrustedRedirects(deadline)).open(request, timeout=IO_TIMEOUT_SECONDS) as response:
            if not _trusted_url(response.geturl()):
                raise TailscaleBootstrapError("Tailscale response is not from a trusted HTTPS source")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_DOWNLOAD_BYTES):
                raise TailscaleBootstrapError("Tailscale download has an invalid or oversized Content-Length")
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TailscaleBootstrapError("Tailscale download exceeded its 120-second time budget")
                chunk = response.read1(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise TailscaleBootstrapError("Tailscale download exceeds the 96 MiB size limit")
                target.write(chunk)
            if total == 0 or (length is not None and total != int(length)):
                raise TailscaleBootstrapError("Tailscale download was empty or truncated")
    except HTTPError as exc:
        raise TailscaleBootstrapError(
            f"Tailscale download failed (HTTP {exc.code}); check pkgs.tailscale.com access and retry."
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        # Do not echo proxy URLs or local credentials.
        raise TailscaleBootstrapError(
            "Tailscale download failed (network, TLS, timeout, or local I/O). "
            "Check connectivity, proxy/TLS configuration, and available disk space; retry "
            "or install Tailscale manually from https://tailscale.com/download."
        ) from exc


def _digest(path: Path) -> str:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise TailscaleBootstrapError("Tailscale cache entry is not a regular file")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TailscaleBootstrapError("Tailscale cache entry is not a regular file")
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise TailscaleBootstrapError("Tailscale file exceeds the 96 MiB size limit")
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, out_tailscale: IO[bytes], out_tailscaled: IO[bytes]) -> None:
    """Extract exactly the two expected regular binaries; nothing else."""
    seen: set[str] = set()
    with tarfile.open(archive, mode="r|gz") as bundle:
        for member in bundle:
            if member.name not in TARMembers:
                raise TailscaleBootstrapError("Tailscale archive has an unexpected member")
            if member.name in seen or not member.isfile():
                raise TailscaleBootstrapError("Tailscale archive has an unsafe or duplicate member")
            if not 0 < member.size <= MAX_DOWNLOAD_BYTES:
                raise TailscaleBootstrapError("Tailscale archive member exceeds the size limit")
            source = bundle.extractfile(member)
            if source is None:
                raise TailscaleBootstrapError("Tailscale archive member could not be read")
            target = out_tailscale if member.name == TARMembers[0] else out_tailscaled
            with source:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise TailscaleBootstrapError("Tailscale archive binary was truncated")
                    target.write(chunk)
                    remaining -= len(chunk)
            seen.add(member.name)
    if seen != set(TARMembers):
        raise TailscaleBootstrapError("Tailscale archive is missing its binaries")


def ensure_tailscale_downloads(*, cache_dir: Path | None = None) -> dict[str, str]:
    """Download/verify the pinned asset; return executable paths.

    Windows/macOS return ``{"installer": <verified installer path>}``; the
    caller decides when to run it (the user consents twice in the wizard).
    Linux returns ``{"tailscale": ..., "tailscaled": ...}`` unpacked user-owned
    binaries; a system-level daemon still requires the official distro steps
    printed by the wizard, or an explicit userspace run.
    """
    asset = tailscale_asset()
    directory = cache_dir if cache_dir is not None else runtime_dir() / "tailscale-bin"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TailscaleBootstrapError("Tailscale cache directory must not be a symlink")
    if os.name != "nt" and (info.st_uid != getattr(os, "getuid")() or info.st_mode & 0o022):
        raise TailscaleBootstrapError(
            "Tailscale cache directory must be owned by this user and not writable by others"
        )
    stem = asset.name
    for suffix in (".exe", ".tgz", ".pkg"):
        if stem.endswith(suffix):
            stem = stem.removesuffix(suffix)
    partials: list[Path] = []
    try:
        if asset.kind == "tgz":
            binary = directory / (stem + "-tailscale")
            daemon = directory / (stem + "-tailscaled")
            if binary.exists() and daemon.exists() and not binary.is_symlink() and not daemon.is_symlink():
                return {"tailscale": str(binary), "tailscaled": str(daemon)}
            archive_dest = directory / asset.name
            if archive_dest.exists() or archive_dest.is_symlink():
                if _digest(archive_dest) != asset.sha256:
                    raise TailscaleBootstrapError(
                        f"Cached Tailscale archive checksum mismatch; refusing to use {archive_dest}."
                    )
            else:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".tailscale-", delete=False) as download:
                    staging = Path(download.name)
                    partials.append(staging)
                    _download(asset, download.file)
                    download.flush()
                    os.fsync(download.fileno())
                if _digest(staging) != asset.sha256:
                    raise TailscaleBootstrapError(
                        "Downloaded Tailscale archive checksum mismatch; refusing to install or execute it"
                    )
                os.replace(staging, archive_dest)
                try:
                    partials.remove(staging)
                except ValueError:
                    pass
            with (
                tempfile.NamedTemporaryFile(dir=directory, prefix=".tailscale-ts-", delete=False) as one,
                tempfile.NamedTemporaryFile(dir=directory, prefix=".tailscale-tsd-", delete=False) as two,
            ):
                out_one, out_two = Path(one.name), Path(two.name)
                partials.extend((out_one, out_two))
                _unpack(archive_dest, one.file, two.file)
                one.flush()
                os.fsync(one.fileno())
                two.flush()
                os.fsync(two.fileno())
            for executable in (out_one, out_two):
                executable.chmod(0o700)
            os.replace(out_one, binary)
            os.replace(out_two, daemon)
            return {"tailscale": str(binary), "tailscaled": str(daemon)}
        destination = directory / asset.name
        if destination.exists() or destination.is_symlink():
            if _digest(destination) != asset.sha256:
                raise TailscaleBootstrapError(
                    f"Cached Tailscale installer checksum mismatch; refusing to use {destination}."
                )
            return {"installer": str(destination)}
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".tailscale-", delete=False) as download:
            installer = Path(download.name)
            partials.append(installer)
            _download(asset, download.file)
            download.flush()
            os.fsync(download.fileno())
        if _digest(installer) != asset.sha256:
            raise TailscaleBootstrapError(
                "Downloaded Tailscale installer checksum mismatch; refusing to run it"
            )
        installer.chmod(0o700)
        os.replace(installer, destination)
        return {"installer": str(destination)}
    except (OSError, tarfile.TarError) as exc:
        raise TailscaleBootstrapError(
            "Could not safely prepare the Tailscale installer (cache I/O or invalid archive). "
            "Check the KaroX runtime directory permissions and disk space, then retry."
        ) from exc
    finally:
        for partial in partials:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                # Never mask the original failure; partials are not executable
                # candidates and are re-created by the next attempt.
                pass


def installer_launch_command(path: Path) -> list[str]:
    """The launch line for a verified installer, derived from the file kind.

    Windows: silent install; the OS elevation prompt is the user's consent
    gate. macOS: the GUI installer opens for the user to click through.
    """
    name = path.name.lower()
    if name.endswith(".exe"):
        return [str(path), "/quiet"]
    if name.endswith(".pkg"):
        return ["open", str(path)]
    raise TailscaleBootstrapError("Linux archives are unpacked, not executed as installers")


def linux_system_install_hint(distro_codename: str = "trixie") -> str:
    """The official repository setup commands for Debian-family hosts."""
    return (
        "sudo mkdir -p --mode=0755 /usr/share/keyrings && "
        f"curl -fsSL https://pkgs.tailscale.com/stable/debian/{distro_codename}.noarmor.gpg | "
        "sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && "
        f"curl -fsSL https://pkgs.tailscale.com/stable/debian/{distro_codename}.tailscale-keyring.list | "
        "sudo tee /etc/apt/sources.list.d/tailscale.list && "
        "sudo apt-get update && sudo apt-get install -y tailscale && "
        "sudo tailscale up"
    )
