"""Lazy, user-local cloudflared provisioning; never installs an OS service.

Pins are from the official cloudflare/cloudflared 2026.7.3 release. GitHub
asset digests identify the macOS *archives*; the release notes identify the
executables inside them. Both are checked. Updating pins is a code change,
not a runtime trust-on-first-use operation.
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
from .tailscale import find_tailscale

CLOUDFLARED_VERSION = "2026.7.3"
MAX_DOWNLOAD_BYTES = 96 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0
IO_TIMEOUT_SECONDS = 15.0
_CHUNK = 64 * 1024
_RELEASE_BASE = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    + CLOUDFLARED_VERSION
    + "/"
)


class TunnelBootstrapError(RuntimeError):
    """A tunnel dependency could not be obtained and verified safely."""


@dataclass(frozen=True)
class CloudflaredAsset:
    name: str
    sha256: str
    executable_sha256: str
    archive: bool = False


def _binary(name: str, digest: str) -> CloudflaredAsset:
    return CloudflaredAsset(name, digest, digest)


ASSETS = {
    ("linux", "amd64"): _binary(
        "cloudflared-linux-amd64",
        "9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17",
    ),
    ("linux", "arm64"): _binary(
        "cloudflared-linux-arm64",
        "65259e652a7bea08bf5df603233ab22b8bf3116af8df9f9206209af6a1b955c0",
    ),
    ("windows", "amd64"): _binary(
        "cloudflared-windows-amd64.exe",
        "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841",
    ),
    ("darwin", "amd64"): CloudflaredAsset(
        "cloudflared-darwin-amd64.tgz",
        "70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b",
        "e88fe5874d42a94f49a7ea59cabc3722d2962d0449232b0f3b1a426a712e275c",
        True,
    ),
    ("darwin", "arm64"): CloudflaredAsset(
        "cloudflared-darwin-arm64.tgz",
        "90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09",
        "f35c50089cd25f77a4cb5a2152036bc26db15aa31fbe11f7995d2e42a4ed6257",
        True,
    ),
}


def select_tunnel(requested: str = "auto", *, tailscale: str | None = None) -> str:
    """Resolve only auto; an explicit provider is never silently substituted.

    Presence is not readiness. Login/Funnel failures remain Tailscale failures,
    rather than unexpectedly publishing a different public endpoint.
    """
    if requested == "auto":
        return "tailscale" if find_tailscale(tailscale) else "cloudflare"
    if requested not in {"tailscale", "cloudflare", "custom"}:
        raise ValueError("web bridge tunnel must be auto, cloudflare, tailscale, or custom")
    return requested


def cloudflared_asset() -> CloudflaredAsset:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        # A 32-bit Python on a 64-bit Windows host must select the host binary.
        machine = os.environ.get("PROCESSOR_ARCHITEW6432", machine).lower()
    arch = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    asset = ASSETS.get((system, arch))
    if asset is None:
        raise TunnelBootstrapError(
            f"No checksum-pinned cloudflared download for {system}/{arch}. "
            "Install a supported cloudflared manually and pass --cloudflared PATH, "
            "or explicitly select --tunnel tailscale."
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
        return (parts.hostname == "github.com" and url.startswith(_RELEASE_BASE)) or (
            parts.hostname == "release-assets.githubusercontent.com"
        )
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
            raise TunnelBootstrapError("cloudflared download exceeded its time budget during redirects")
        if not _trusted_url(newurl):
            raise TunnelBootstrapError("cloudflared download redirected outside trusted HTTPS sources")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(asset: CloudflaredAsset, target: IO[bytes]) -> None:
    url = _RELEASE_BASE + asset.name
    if not _trusted_url(url):
        raise TunnelBootstrapError("cloudflared download source is not trusted HTTPS")
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    request = Request(url, headers={"User-Agent": "KaroX-tunnel-bootstrap", "Accept-Encoding": "identity"})
    try:
        with build_opener(_TrustedRedirects(deadline)).open(request, timeout=IO_TIMEOUT_SECONDS) as response:
            if not _trusted_url(response.geturl()):
                raise TunnelBootstrapError("cloudflared response is not from a trusted HTTPS source")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_DOWNLOAD_BYTES):
                raise TunnelBootstrapError("cloudflared download has an invalid or oversized Content-Length")
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TunnelBootstrapError("cloudflared download exceeded its 120-second time budget")
                # read1 does at most one underlying read: trickled bytes cannot
                # restart an unbounded read(n). Each read also has a 15s timeout.
                chunk = response.read1(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise TunnelBootstrapError("cloudflared download exceeds the 96 MiB size limit")
                target.write(chunk)
            if total == 0 or (length is not None and total != int(length)):
                raise TunnelBootstrapError("cloudflared download was empty or truncated")
    except HTTPError as exc:
        raise TunnelBootstrapError(
            f"cloudflared download failed (HTTP {exc.code}); check GitHub access and retry."
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        # Do not echo proxy URLs, signed redirect queries, or local credentials.
        raise TunnelBootstrapError(
            "cloudflared download failed (network, TLS, timeout, or local I/O). "
            "Check connectivity, proxy/TLS configuration, and available disk space; retry "
            "or install cloudflared manually with --cloudflared PATH."
        ) from exc


def _digest(path: Path) -> str:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise TunnelBootstrapError("cloudflared cache entry is not a regular file")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TunnelBootstrapError("cloudflared cache entry is not a regular file")
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise TunnelBootstrapError("cloudflared file exceeds the 96 MiB size limit")
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, target: IO[bytes]) -> None:
    """Copy exactly one regular file; never extract archive paths or links."""
    found = False
    with tarfile.open(archive, mode="r|gz") as bundle:
        for member in bundle:
            if found or member.name != "cloudflared" or not member.isfile():
                raise TunnelBootstrapError("cloudflared archive has an unsafe or unexpected member")
            if not 0 < member.size <= MAX_DOWNLOAD_BYTES:
                raise TunnelBootstrapError("cloudflared archive member exceeds the size limit")
            source = bundle.extractfile(member)
            if source is None:
                raise TunnelBootstrapError("cloudflared archive is missing its executable")
            with source:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise TunnelBootstrapError("cloudflared archive executable was truncated")
                    target.write(chunk)
                    remaining -= len(chunk)
            found = True
    if not found:
        raise TunnelBootstrapError("cloudflared archive is missing its executable")


def ensure_cloudflared(*, cache_dir: Path | None = None) -> str:
    """Return a verified cached executable, or download and atomically publish it.

    Only called when the launcher finds no user-installed cloudflared. Cached
    files are checked on every use. No network, subprocess, or OS installation
    is performed at import, inspection, or automatic provider selection time.
    """
    asset = cloudflared_asset()
    directory = cache_dir if cache_dir is not None else runtime_dir() / "bin"
    stem = asset.name.removesuffix(".tgz").removesuffix(".exe")
    filename = stem + "-" + CLOUDFLARED_VERSION
    if asset.name.endswith(".exe"):
        filename += ".exe"
    destination = directory / filename
    partials: list[Path] = []
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TunnelBootstrapError("cloudflared cache directory must not be a symlink")
        if os.name != "nt" and (info.st_uid != getattr(os, "getuid")() or info.st_mode & 0o022):
            raise TunnelBootstrapError("cloudflared cache directory must be owned by this user and not writable by others")
        if destination.exists() or destination.is_symlink():
            if _digest(destination) != asset.executable_sha256:
                raise TunnelBootstrapError(
                    f"Cached cloudflared checksum mismatch; refusing to execute {destination}. "
                    "Remove that cache entry and retry, or pass --cloudflared PATH."
                )
            destination.chmod(0o700)
            return str(destination)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".cloudflared-", delete=False) as download:
            archive = Path(download.name)
            partials.append(archive)
            _download(asset, download.file)
            download.flush()
            os.fsync(download.fileno())
        if _digest(archive) != asset.sha256:
            raise TunnelBootstrapError("Downloaded cloudflared checksum mismatch; refusing to install or execute it")
        executable = archive
        if asset.archive:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".cloudflared-", delete=False) as unpacked:
                executable = Path(unpacked.name)
                partials.append(executable)
                _unpack(archive, unpacked.file)
                unpacked.flush()
                os.fsync(unpacked.fileno())
        if _digest(executable) != asset.executable_sha256:
            raise TunnelBootstrapError("cloudflared executable checksum mismatch; refusing to install or execute it")
        executable.chmod(0o700)
        os.replace(executable, destination)
        return str(destination)
    except (OSError, tarfile.TarError) as exc:
        raise TunnelBootstrapError(
            "Could not safely prepare cloudflared (cache I/O or invalid archive). "
            "Check the KaroX runtime directory permissions and disk space, then retry."
        ) from exc
    finally:
        for partial in partials:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                # Never mask the original failure; partials are not executable
                # candidates and will never be used by the resolver.
                pass
