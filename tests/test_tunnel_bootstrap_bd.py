"""Offline dependency/bootstrap tests: no binaries run or OS installs performed."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from karox import tunnel_bootstrap as tb
from karox import web_bridge_launcher as launcher

PAYLOAD = b"verified executable fixture, never run"


def asset_for(payload: bytes = PAYLOAD, *, name: str = "cloudflared-linux-amd64") -> tb.CloudflaredAsset:
    digest = hashlib.sha256(payload).hexdigest()
    return tb.CloudflaredAsset(name, digest, digest)


def fake_asset(monkeypatch, asset=None):
    selected = asset or asset_for()
    monkeypatch.setattr(tb, "cloudflared_asset", lambda: selected)
    return selected


@pytest.mark.parametrize(
    ("system", "machine", "name"),
    [
        ("Linux", "x86_64", "cloudflared-linux-amd64"),
        ("Linux", "aarch64", "cloudflared-linux-arm64"),
        ("Darwin", "arm64", "cloudflared-darwin-arm64.tgz"),
        ("Darwin", "x86_64", "cloudflared-darwin-amd64.tgz"),
        ("Windows", "AMD64", "cloudflared-windows-amd64.exe"),
    ],
)
def test_platform_pins(monkeypatch, system, machine, name):
    monkeypatch.setattr(tb.platform, "system", lambda: system)
    monkeypatch.setattr(tb.platform, "machine", lambda: machine)
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    asset = tb.cloudflared_asset()
    assert asset.name == name
    assert len(asset.sha256) == len(asset.executable_sha256) == 64
    assert int(asset.sha256, 16) > 0


def test_windows_host_architecture_not_emulated_python(monkeypatch):
    monkeypatch.setattr(tb.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tb.platform, "machine", lambda: "x86")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "AMD64")
    assert tb.cloudflared_asset().name.endswith("amd64.exe")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    with pytest.raises(tb.TunnelBootstrapError, match="windows/arm64.*--cloudflared"):
        tb.cloudflared_asset()


@pytest.mark.parametrize("system,machine", [("FreeBSD", "x86_64"), ("Linux", "riscv64"), ("Darwin", "i386")])
def test_unsupported_platform_does_not_download(monkeypatch, tmp_path, system, machine):
    monkeypatch.setattr(tb.platform, "system", lambda: system)
    monkeypatch.setattr(tb.platform, "machine", lambda: machine)
    download = Mock()
    monkeypatch.setattr(tb, "_download", download)
    with pytest.raises(tb.TunnelBootstrapError, match="No checksum-pinned"):
        tb.ensure_cloudflared(cache_dir=tmp_path)
    download.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("installed,expected", [(None, "cloudflare"), ("/installed/tailscale", "tailscale")])
def test_auto_uses_presence_without_network(monkeypatch, installed, expected, tmp_path):
    lookup = Mock(return_value=installed)
    monkeypatch.setattr(tb, "find_tailscale", lookup)
    assert tb.select_tunnel() == expected
    config = launcher.WebBridgeConnectConfig(profile="chatgpt-web", repository=tmp_path)
    assert config.tunnel == expected
    assert config.local_health_interval_seconds == (0.0 if expected == "tailscale" else 10.0)


@pytest.mark.parametrize("requested", ["tailscale", "cloudflare", "custom"])
def test_explicit_provider_never_substituted(monkeypatch, requested):
    lookup = Mock(side_effect=AssertionError("explicit provider must not probe"))
    monkeypatch.setattr(tb, "find_tailscale", lookup)
    assert tb.select_tunnel(requested) == requested


def test_invalid_provider_rejected():
    with pytest.raises(ValueError, match="auto"):
        tb.select_tunnel("typo")


def test_download_verified_then_atomic_and_cache_rechecked(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    calls = []

    def download(asset, target):
        assert not list(tmp_path.glob("cloudflared-*"))
        target.write(PAYLOAD)
        calls.append(asset)

    monkeypatch.setattr(tb, "_download", download)
    path = Path(tb.ensure_cloudflared(cache_dir=tmp_path))
    assert path.read_bytes() == PAYLOAD
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert tb.ensure_cloudflared(cache_dir=tmp_path) == str(path)
    assert len(calls) == 1
    path.write_bytes(b"tampered cached executable")
    with pytest.raises(tb.TunnelBootstrapError, match="Cached cloudflared checksum mismatch"):
        tb.ensure_cloudflared(cache_dir=tmp_path)
    assert len(calls) == 1
    assert not list(tmp_path.glob(".cloudflared-*"))


def test_checksum_failure_never_replaces_or_chmods(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    monkeypatch.setattr(tb, "_download", lambda asset, target: target.write(b"corrupt"))
    replace = Mock()
    chmod = Mock()
    monkeypatch.setattr(tb.os, "replace", replace)
    monkeypatch.setattr(Path, "chmod", chmod)
    with pytest.raises(tb.TunnelBootstrapError, match="checksum mismatch"):
        tb.ensure_cloudflared(cache_dir=tmp_path)
    replace.assert_not_called()
    chmod.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_interrupted_download_leaves_no_candidates(monkeypatch, tmp_path):
    fake_asset(monkeypatch)

    def fail(asset, target):
        target.write(PAYLOAD[:6])
        raise tb.TunnelBootstrapError("offline")

    monkeypatch.setattr(tb, "_download", fail)
    with pytest.raises(tb.TunnelBootstrapError, match="offline"):
        tb.ensure_cloudflared(cache_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_cached_symlink_is_refused(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    monkeypatch.setattr(tb, "_download", lambda asset, target: target.write(PAYLOAD))
    path = Path(tb.ensure_cloudflared(cache_dir=tmp_path))
    path.unlink()
    other = tmp_path / "other"
    other.write_bytes(PAYLOAD)
    try:
        path.symlink_to(other)
    except OSError:
        pytest.skip("symlinks unavailable for this user")
    with pytest.raises(tb.TunnelBootstrapError, match="not a regular file"):
        tb.ensure_cloudflared(cache_dir=tmp_path)


def archive_bytes(name="cloudflared", kind=tarfile.REGTYPE, extra=False):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as bundle:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = len(PAYLOAD) if kind == tarfile.REGTYPE else 0
        member.linkname = "../../escape"
        bundle.addfile(member, io.BytesIO(PAYLOAD) if member.isfile() else None)
        if extra:
            bundle.addfile(tarfile.TarInfo("extra"), io.BytesIO())
    return out.getvalue()


def provision_archive(monkeypatch, tmp_path, payload, executable_digest=None):
    asset = tb.CloudflaredAsset(
        "cloudflared-darwin-amd64.tgz", hashlib.sha256(payload).hexdigest(),
        executable_digest or hashlib.sha256(PAYLOAD).hexdigest(), True,
    )
    fake_asset(monkeypatch, asset)
    monkeypatch.setattr(tb, "_download", lambda asset, target: target.write(payload))
    return tb.ensure_cloudflared(cache_dir=tmp_path)


def test_macos_copies_only_verified_regular_binary(monkeypatch, tmp_path):
    path = Path(provision_archive(monkeypatch, tmp_path, archive_bytes()))
    assert path.read_bytes() == PAYLOAD
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("name,kind,extra", [
    ("../escape", tarfile.REGTYPE, False),
    ("/cloudflared", tarfile.REGTYPE, False),
    ("cloudflared", tarfile.SYMTYPE, False),
    ("cloudflared", tarfile.LNKTYPE, False),
    ("cloudflared", tarfile.FIFOTYPE, False),
    ("cloudflared", tarfile.REGTYPE, True),
])
def test_unsafe_archive_never_installed(monkeypatch, tmp_path, name, kind, extra):
    with pytest.raises(tb.TunnelBootstrapError, match="unsafe or unexpected"):
        provision_archive(monkeypatch, tmp_path, archive_bytes(name, kind, extra))
    assert not list(tmp_path.iterdir())


def test_archive_executable_gets_independent_checksum(monkeypatch, tmp_path):
    with pytest.raises(tb.TunnelBootstrapError, match="executable checksum mismatch"):
        provision_archive(monkeypatch, tmp_path, archive_bytes(), "0" * 64)
    assert not list(tmp_path.iterdir())


class Response(io.BytesIO):
    def __init__(self, payload=PAYLOAD, headers=None, url=None):
        super().__init__(payload)
        self.headers = headers or {}
        self.url = url or tb._RELEASE_BASE + "cloudflared-linux-amd64"

    def geturl(self):
        return self.url


def fake_response(monkeypatch, response):
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(tb, "build_opener", lambda *args: opener)
    return opener


def test_download_uses_https_and_bounded_io(monkeypatch):
    opener = fake_response(monkeypatch, Response(headers={"Content-Length": str(len(PAYLOAD))}))
    output = io.BytesIO()
    tb._download(asset_for(), output)
    assert output.getvalue() == PAYLOAD
    args, kwargs = opener.open.call_args
    assert args[0].full_url.startswith(tb._RELEASE_BASE)
    assert kwargs["timeout"] == tb.IO_TIMEOUT_SECONDS


@pytest.mark.parametrize("headers,payload,limit", [
    ({"Content-Length": "999999999"}, b"", tb.MAX_DOWNLOAD_BYTES),
    ({"Content-Length": "invalid"}, b"", tb.MAX_DOWNLOAD_BYTES),
    ({"Content-Length": "100"}, PAYLOAD, tb.MAX_DOWNLOAD_BYTES),
    ({}, PAYLOAD, 5),
    ({}, b"", tb.MAX_DOWNLOAD_BYTES),
])
def test_bad_length_truncation_and_chunked_size_limit(monkeypatch, headers, payload, limit):
    fake_response(monkeypatch, Response(payload, headers))
    monkeypatch.setattr(tb, "MAX_DOWNLOAD_BYTES", limit)
    with pytest.raises(tb.TunnelBootstrapError):
        tb._download(asset_for(), io.BytesIO())


@pytest.mark.parametrize("url", [
    "http://github.com/cloudflare/cloudflared/releases/download/2026.7.3/file",
    "https://github.com.evil.example/file", "https://evil.example/file",
    "https://user:secret@release-assets.githubusercontent.com/file",
    "https://release-assets.githubusercontent.com:8080/file",
    "https://release-assets.githubusercontent.com:bad/file",
    "https://github.com/somewhere-else/file",
])
def test_untrusted_redirect_refused_before_following(url):
    redirects = tb._TrustedRedirects(time.monotonic() + 120)
    with pytest.raises(tb.TunnelBootstrapError, match="trusted HTTPS"):
        redirects.redirect_request(Request(tb._RELEASE_BASE + "file"), None, 302, "Found", {}, url)


def test_transfer_budget_stops_trickled_data(monkeypatch):
    fake_response(monkeypatch, Response())
    ticks = iter([0.0, 121.0])
    monkeypatch.setattr(tb.time, "monotonic", lambda: next(ticks))
    with pytest.raises(tb.TunnelBootstrapError, match="time budget"):
        tb._download(asset_for(), io.BytesIO())


@pytest.mark.parametrize("error", [URLError("https://user:secret@proxy.invalid"), HTTPError("https://host/?secret", 403, "forbidden", {}, None), TimeoutError()])
def test_download_errors_meaningful_and_do_not_echo_credentials(monkeypatch, error):
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(tb, "build_opener", lambda *args: opener)
    with pytest.raises(tb.TunnelBootstrapError) as exc:
        tb._download(asset_for(), io.BytesIO())
    assert "failed" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_bad_explicit_path_never_triggers_download(monkeypatch):
    monkeypatch.setattr(launcher, "find_cloudflared", lambda explicit: None)
    ensure = Mock()
    monkeypatch.setattr(launcher, "ensure_cloudflared", ensure)
    with pytest.raises(launcher.WebBridgeLaunchError, match="--cloudflared"):
        launcher.start_cloudflare_quick_tunnel(8765, executable="missing-custom-executable")
    ensure.assert_not_called()


def test_lazy_download_error_propagates_before_spawn(monkeypatch):
    monkeypatch.setattr(launcher, "find_cloudflared", lambda explicit: None)
    monkeypatch.setattr(launcher, "ensure_cloudflared", Mock(side_effect=tb.TunnelBootstrapError("checksum mismatch")))
    popen = Mock()
    with pytest.raises(launcher.WebBridgeLaunchError, match="checksum mismatch"):
        launcher.start_cloudflare_quick_tunnel(8765, popen=popen)
    popen.assert_not_called()


@pytest.mark.parametrize("installed", [None, "/existing/cloudflared"])
def test_launcher_uses_existing_or_lazy_verified_binary(monkeypatch, installed):
    monkeypatch.setattr(launcher, "find_cloudflared", lambda explicit: installed)
    ensure = Mock(return_value="/verified/cache/cloudflared")
    monkeypatch.setattr(launcher, "ensure_cloudflared", ensure)
    monkeypatch.setattr(launcher, "_child_options", lambda: {})
    process = Mock(stdout=io.StringIO("https://test.trycloudflare.com\n"))
    process.poll.return_value = None
    popen = Mock(return_value=process)
    tunnel = launcher.start_cloudflare_quick_tunnel(8765, popen=popen)
    tunnel.reader.join(2)
    assert popen.call_args.args[0][0] == (installed or ensure.return_value)
    assert tunnel.public_url == "https://test.trycloudflare.com"
    assert ensure.call_count == (0 if installed else 1)


def test_cache_directory_symlink_refused(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(real, target_is_directory=True)
    with pytest.raises(tb.TunnelBootstrapError, match="symlink"):
        tb.ensure_cloudflared(cache_dir=cache)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_cache_directory_writable_by_others_refused(monkeypatch, tmp_path):
    fake_asset(monkeypatch)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o777)
    cache.chmod(0o777)
    with pytest.raises(tb.TunnelBootstrapError, match="not writable by others"):
        tb.ensure_cloudflared(cache_dir=cache)
