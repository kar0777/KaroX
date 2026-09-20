"""Offline Tailscale bootstrap tests: no binaries run and no OS installs happen."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path
from unittest.mock import Mock  # noqa: F401  (kept for parity with the bd-test style)

import pytest

from karox import tailscale_bootstrap as tsb

PAYLOAD = b"verified tailscale payload, never executed"
DAEMON = b"verified tailscaled payload, never executed"


def asset_for(payload: bytes = PAYLOAD, *, name: str = "tailscale_1.102.4_amd64.tgz", kind: str = "tgz") -> tsb.TailscaleAsset:
    digest = hashlib.sha256(payload).hexdigest()
    return tsb.TailscaleAsset(name, digest, kind)


def fake_asset(monkeypatch, asset=None):
    selected = asset or asset_for()
    monkeypatch.setattr(tsb, "tailscale_asset", lambda: selected)
    return selected


def fake_download(monkeypatch, payload: bytes):
    """Replace the network layer with an in-memory writer."""
    monkeypatch.setattr(tsb, "_download", lambda asset, target: target.write(payload))


@pytest.mark.parametrize(
    ("system", "machine", "name", "kind"),
    [
        ("Linux", "x86_64", "tailscale_1.102.4_amd64.tgz", "tgz"),
        ("Linux", "aarch64", "tailscale_1.102.4_arm64.tgz", "tgz"),
        ("Darwin", "x86_64", "Tailscale-1.102.4-macos.pkg", "pkg"),
        ("Darwin", "arm64", "Tailscale-1.102.4-macos.pkg", "pkg"),
        ("Windows", "AMD64", "tailscale-setup-full-1.102.4.exe", "exe"),
        ("Windows", "ARM64", "tailscale-setup-full-1.102.4.exe", "exe"),
    ],
)
def test_platform_pins(monkeypatch, system, machine, name, kind):
    monkeypatch.setattr(tsb.platform, "system", lambda: system)
    monkeypatch.setattr(tsb.platform, "machine", lambda: machine)
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    asset = tsb.tailscale_asset()
    assert asset.name == name
    assert asset.kind == kind
    assert len(asset.sha256) == 64
    assert int(asset.sha256, 16) > 0


def test_windows_host_architecture_not_emulated_python(monkeypatch):
    monkeypatch.setattr(tsb.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tsb.platform, "machine", lambda: "x86")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "AMD64")
    assert tsb.tailscale_asset().name.endswith("1.102.4.exe")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    assert tsb.tailscale_asset().name.endswith("1.102.4.exe")


def test_unsupported_platform_is_rejected(monkeypatch):
    monkeypatch.setattr(tsb.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(tsb.platform, "machine", lambda: "mips")
    with pytest.raises(tsb.TailscaleBootstrapError, match="checksum-pinned"):
        tsb.tailscale_asset()


@pytest.mark.parametrize(
    "url",
    [
        "http://pkgs.tailscale.com/stable/x",
        "https://evil.example.com/stable/x",
        "https://pkgs.tailscale.com/unstable/x",
        "https://pkgs.tailscale.com:8080/stable/x",
        "https://user:pass@pkgs.tailscale.com/stable/x",
        "https://pkgs.tailscale.com/stable/x#fragment",
    ],
)
def test_untrusted_urls_rejected(url):
    assert tsb._trusted_url(url) is False


def test_trusted_url_accepted():
    assert tsb._trusted_url(tsb.PACKAGES_BASE + "tailscale_1.102.4_amd64.tgz") is True


def test_redirect_outside_trusted_host_rejected(monkeypatch):
    redirects = tsb._TrustedRedirects(deadline=tsb.time.monotonic() + 60)
    request = Mock()
    with pytest.raises(tsb.TailscaleBootstrapError, match="outside trusted"):
        redirects.redirect_request(request, None, 302, "Found", {}, "https://evil.example.com/x")


def test_linux_unpack_writes_two_binaries(monkeypatch, tmp_path):
    payload_one = b"tailscale binary"
    payload_two = b"tailscaled binary"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in (("tailscale/tailscale", payload_one), ("tailscale/tailscaled", payload_two)):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(payload))
    fake_asset(monkeypatch, asset_for(buffer.getvalue()))
    fake_download(monkeypatch, buffer.getvalue())
    monkeypatch.setattr(tsb.platform, "system", lambda: "Linux")

    result = tsb.ensure_tailscale_downloads(cache_dir=tmp_path)

    assert set(result) == {"tailscale", "tailscaled"}
    binary = Path(result["tailscale"])
    daemon = Path(result["tailscaled"])
    assert binary.read_bytes() == payload_one
    assert daemon.read_bytes() == payload_two
    if os.name != "nt":
        assert stat.S_IMODE(binary.stat().st_mode) & 0o111
    # The verified archive is kept alongside the binaries for re-verification.
    assert (tmp_path / "tailscale_1.102.4_amd64.tgz").exists()


def test_linux_cached_binaries_short_circuit(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in (("tailscale/tailscale", b"tailscale binary"), ("tailscale/tailscaled", b"tailscaled binary")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    fake_asset(monkeypatch, asset_for(buffer.getvalue()))
    fake_download(monkeypatch, buffer.getvalue())
    monkeypatch.setattr(tsb.platform, "system", lambda: "Linux")
    first = tsb.ensure_tailscale_downloads(cache_dir=tmp_path)
    # Remove the archive; a cached verified pair must still be reused.
    (tmp_path / "tailscale_1.102.4_amd64.tgz").unlink()
    def boom(asset, target):  # pragma: no cover - must not be called
        raise AssertionError("network must not be used for a cached pair")
    monkeypatch.setattr(tsb, "_download", boom)
    second = tsb.ensure_tailscale_downloads(cache_dir=tmp_path)
    assert first == second


def test_truncated_download_rejected(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in (("tailscale/tailscale", b"tailscale binary"), ("tailscale/tailscaled", b"tailscaled")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    full = buffer.getvalue()
    fake_asset(monkeypatch, asset_for(full))
    # A truncated download: the archive cut in half, never completing.
    fake_download(monkeypatch, full[: len(full) // 2])
    monkeypatch.setattr(tsb.platform, "system", lambda: "Linux")
    with pytest.raises(tsb.TailscaleBootstrapError, match="checksum mismatch"):
        tsb.ensure_tailscale_downloads(cache_dir=tmp_path)


def test_windows_installer_cached_and_verified(monkeypatch, tmp_path):
    payload = b"msi bootstrapper payload"
    asset = asset_for(payload, name="tailscale-setup-full-1.102.4.exe", kind="exe")
    fake_asset(monkeypatch, asset)
    fake_download(monkeypatch, payload)
    monkeypatch.setattr(tsb.platform, "system", lambda: "Windows")
    result = tsb.ensure_tailscale_downloads(cache_dir=tmp_path)
    assert set(result) == {"installer"}
    assert Path(result["installer"]).read_bytes() == payload
    assert Path(result["installer"]).stat().st_mode & 0o111


def test_cached_installer_digest_mismatch_refused(monkeypatch, tmp_path):
    asset = asset_for(b"payload", name="tailscale-setup-full-1.102.4.exe", kind="exe")
    fake_asset(monkeypatch, asset)
    monkeypatch.setattr(tsb.platform, "system", lambda: "Windows")
    (tmp_path / asset.name).write_bytes(b"tampered payload")
    with pytest.raises(tsb.TailscaleBootstrapError, match="checksum mismatch"):
        tsb.ensure_tailscale_downloads(cache_dir=tmp_path)


def test_installer_launch_command():
    assert tsb.installer_launch_command(Path("x/setup.exe")) == [str(Path("x/setup.exe")), "/quiet"]
    assert tsb.installer_launch_command(Path("x/Tailscale.pkg")) == ["open", str(Path("x/Tailscale.pkg"))]
    with pytest.raises(tsb.TailscaleBootstrapError):
        tsb.installer_launch_command(Path("x/bundle.tgz"))


def test_linux_hint_names_the_official_repository():
    hint = tsb.linux_system_install_hint("noble")
    assert "noble.noarmor.gpg" in hint
    assert "apt-get install -y tailscale" in hint
    assert "sudo tailscale up" in hint


def test_symlinked_cache_directory_rejected(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    monkeypatch.setattr(tsb.platform, "system", lambda: "Linux")
    link = tmp_path / "link"
    target = tmp_path / "real"
    target.mkdir()
    os.symlink(target, link)
    with pytest.raises(tsb.TailscaleBootstrapError, match="symlink"):
        tsb.ensure_tailscale_downloads(cache_dir=link)
