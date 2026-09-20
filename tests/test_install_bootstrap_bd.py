"""Bootstrap fixture installs stay entirely in pytest temporary directories.

No network, package installs, managed Python provisioning, tunnel execution or
user PATH mutation occurs. PowerShell runtime tests are optional when available.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from _support import ROOT

TAG = "v5.0.0rc3"
BUNDLE = f"KaroX-{TAG}-linux-x64-portable"


def portable_fixture(path: Path, *, bad_member: str | None = None, link=False, missing_uv=False):
    with tarfile.open(path, "w:gz") as archive:
        members = [(f"{BUNDLE}/karox", b"#!/bin/sh\nexit 99\n")]
        if not missing_uv:
            members.append((f"{BUNDLE}/uv", b"not a real runtime"))
        if bad_member:
            members.append((bad_member, b"malicious"))
        for name, content in members:
            member = tarfile.TarInfo(name)
            if link and name == bad_member:
                member.type = tarfile.SYMTYPE
                member.linkname = "../../escape"
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))


@pytest.fixture
def posix_bootstrap(tmp_path):
    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("POSIX bash fixture test")
    script = tmp_path / "bootstrap.sh"
    script.write_text((ROOT / "bootstrap.sh").read_text(encoding="utf-8-sig"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Delegate no real network. Enforce the transfer contract in the fake curl.
    curl = bin_dir / "curl"
    curl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
assert args[args.index('--proto') + 1] == '=https'
assert args[args.index('--proto-redir') + 1] == '=https'
assert 0 < int(args[args.index('--max-time') + 1]) <= 120
assert args[args.index('--connect-timeout') + 1] == '15'
limit = int(args[args.index('--max-filesize') + 1])
assert 0 < limit <= 268435456
url = next(arg for arg in args if arg.startswith('https://'))
output = pathlib.Path(args[args.index('-o') + 1])
fixture = pathlib.Path(os.environ['FIXTURE_ROOT'])
with (fixture / 'requests.jsonl').open('a') as log:
    log.write(json.dumps(url) + '\\n')
kind = 'manifest' if url.endswith('.json') else ('checksums' if url.endswith('.txt') else 'archive')
status = os.environ.get('STATUS_' + kind.upper(), '200')
if status == 'timeout':
    sys.exit(28)
source = fixture / kind
if source.exists():
    shutil.copyfile(source, output)
else:
    output.write_bytes(b'')
print(status, end='')
''')
    curl.chmod(0o700)
    uname = bin_dir / "uname"
    uname.write_text('#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n')
    uname.chmod(0o700)
    portable_fixture(tmp_path / "archive")
    digest = hashlib.sha256((tmp_path / "archive").read_bytes()).hexdigest()
    (tmp_path / "checksums").write_text(f"{digest}  {BUNDLE}.tar.gz\n")
    (tmp_path / "manifest").write_text(json.dumps({"tag": TAG}))
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "install root"
    (install / "portable").mkdir(parents=True)
    (install / "portable" / "old-install").write_text("preserve on failure")
    env = dict(os.environ, HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               KAROX_INSTALL_ROOT=str(install), KAROX_BOOTSTRAP_REF=TAG, KAROX_NO_START="1",
               FIXTURE_ROOT=str(tmp_path), TMPDIR=str(tmp_path))
    env.pop("KAROX_SOURCE_SHA256", None)
    return script, env, install


def run_bootstrap(fixture, *args):
    script, env, _ = fixture
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=15)


def assert_previous_install(fixture):
    _, _, install = fixture
    assert (install / "portable" / "old-install").read_text() == "preserve on failure"
    assert not list(install.glob(".portable-new.*"))
    assert not (install / "source").exists()


def test_posix_verified_portable_install_without_running_it(posix_bootstrap):
    result = run_bootstrap(posix_bootstrap, "--channel", "preview")
    assert result.returncode == 0, result.stderr
    _, env, install = posix_bootstrap
    assert (install / "portable" / "karox").is_file()
    assert (install / "portable" / "uv").is_file()
    assert not (install / "portable" / "old-install").exists()
    assert (Path(env["HOME"]) / ".local/bin/karox").is_symlink()
    assert 'export PATH="$HOME/.local/bin:$PATH"' in result.stdout
    assert not list(install.glob(".portable-*"))
    assert not list(Path(env["TMPDIR"]).glob("karox-bootstrap.*"))


@pytest.mark.parametrize("status", ["403", "500", "timeout"])
def test_posix_download_error_is_not_source_fallback(posix_bootstrap, status):
    posix_bootstrap[1]["STATUS_ARCHIVE"] = status
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "download" in result.stderr.lower()
    assert "falling back" not in result.stderr
    assert_previous_install(posix_bootstrap)


def test_posix_404_requires_explicit_source_checksum(posix_bootstrap):
    posix_bootstrap[1]["STATUS_ARCHIVE"] = "404"
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "KAROX_SOURCE_SHA256" in result.stderr
    assert_previous_install(posix_bootstrap)


@pytest.mark.parametrize("status", ["404", "403", "timeout"])
def test_posix_checksum_download_error_is_fatal(posix_bootstrap, status):
    posix_bootstrap[1]["STATUS_CHECKSUMS"] = status
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "refusing source fallback" in result.stderr
    assert_previous_install(posix_bootstrap)


@pytest.mark.parametrize("entry", ["", "not-a-hash", "0" * 64])
def test_posix_bad_checksum_preserves_existing_install(posix_bootstrap, entry):
    fixture = Path(posix_bootstrap[1]["FIXTURE_ROOT"])
    (fixture / "checksums").write_text(f"{entry}  {BUNDLE}.tar.gz\n")
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "checksum" in result.stderr
    assert_previous_install(posix_bootstrap)


@pytest.mark.parametrize("member,link", [
    (f"{BUNDLE}/../escape", False), ("/absolute", False),
    (f"{BUNDLE}/evil-link", True), (f"{BUNDLE}/nested/unexpected", False),
    (f"{BUNDLE}/karox", False),
])
def test_posix_even_checksummed_unsafe_archive_is_refused(posix_bootstrap, member, link):
    fixture = Path(posix_bootstrap[1]["FIXTURE_ROOT"])
    portable_fixture(fixture / "archive", bad_member=member, link=link)
    digest = hashlib.sha256((fixture / "archive").read_bytes()).hexdigest()
    (fixture / "checksums").write_text(f"{digest}  {BUNDLE}.tar.gz\n")
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "unsafe" in result.stderr
    assert_previous_install(posix_bootstrap)


def test_posix_partial_bundle_never_replaces_install(posix_bootstrap):
    fixture = Path(posix_bootstrap[1]["FIXTURE_ROOT"])
    portable_fixture(fixture / "archive", missing_uv=True)
    digest = hashlib.sha256((fixture / "archive").read_bytes()).hexdigest()
    (fixture / "checksums").write_text(f"{digest}  {BUNDLE}.tar.gz\n")
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "runtime is missing" in result.stderr
    assert_previous_install(posix_bootstrap)


def test_posix_manifest_failure_never_installs_main(posix_bootstrap):
    posix_bootstrap[1].pop("KAROX_BOOTSTRAP_REF")
    posix_bootstrap[1]["STATUS_MANIFEST"] = "timeout"
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "no unverified branch fallback" in result.stderr
    assert_previous_install(posix_bootstrap)


def test_posix_resolve_only_has_no_download_or_install(posix_bootstrap):
    result = run_bootstrap(posix_bootstrap, "--resolve-only")
    assert result.returncode == 0
    assert result.stdout.strip() == TAG
    assert not (Path(posix_bootstrap[1]["FIXTURE_ROOT"]) / "requests.jsonl").exists()
    assert_previous_install(posix_bootstrap)


def test_powershell_bootstrap_streams_bounds_verifies_and_sets_path_before_start():
    source = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    assert "$handler.AllowAutoRedirect = $false" in source
    assert "$cancel.CancelAfter(120000)" in source
    assert "$total -gt $MaxBytes" in source
    assert "Get-FileHash -Algorithm SHA256" in source
    assert "Expand-Archive" not in source
    assert "$kind -notin @(0, 16384, 32768)" in source
    assert "[IO.FileMode]::CreateNew" in source
    assert source.index("        Enable-KaroXPath") < source.index("if (Install-PortableIfAvailable)")
    assert "[Environment]::SetEnvironmentVariable('Path'" in source
    assert "$env:Path = \"$AppRoot;$env:Path\"" in source
    assert "original window" in source
    assert '"%~dp0portable\\karox.cmd" %*' in source


def test_powershell_script_parses_without_installation_when_available():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell unavailable")
    script = str(ROOT / "bootstrap.ps1").replace("'", "''")
    result = subprocess.run([executable, "-NoProfile", "-Command", f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}')) | Out-Null"],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def powershell_executable():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell unavailable")
    return executable


def powershell_functions(names):
    script = str(ROOT / "bootstrap.ps1").replace("'", "''")
    selected = ",".join("'" + name + "'" for name in names)
    return f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$ast = [scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}')).Ast
$wanted = @({selected})
$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $wanted }}, $true) | ForEach-Object {{
    . ([scriptblock]::Create($_.Extent.Text))
}}
"""


def ps_literal(path):
    return "'" + str(path).replace("'", "''") + "'"


@pytest.mark.parametrize("member,symlink", [
    ("bundle/../escape", False), ("/absolute", False), ("bundle/evil:stream", False),
    ("bundle/CON.txt", False), ("bundle/file.", False), ("bundle/back\\slash", False),
    ("bundle/symlink", True), ("bundle/uv.exe", False),
])
def test_powershell_safe_zip_refuses_unsafe_members(tmp_path, member, symlink):
    import warnings
    import zipfile

    executable = powershell_executable()
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bundle/uv.exe", b"fixture")
        info = zipfile.ZipInfo(member)
        if symlink:
            info.create_system = 3
            info.external_attr = 0o120777 << 16
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr(info, b"unsafe")
    output = tmp_path / "out"
    command = powershell_functions(["Expand-KaroXSafeZip"]) + f"\nExpand-KaroXSafeZip -Archive {ps_literal(path)} -Destination {ps_literal(output)} -PortableRoot 'bundle'"
    result = subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode != 0
    assert "Archive" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("scenario", ["success", "checksum-mismatch", "offline", "404", "missing-uv"])
def test_powershell_portable_activation_with_mock_downloads(tmp_path, scenario):
    import zipfile

    executable = powershell_executable()
    fixture = tmp_path / "fixture.zip"
    root = f"KaroX-{TAG}-windows-x64-portable"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr(f"{root}/karox.cmd", "@exit /b 99")
        if scenario != "missing-uv":
            archive.writestr(f"{root}/uv.exe", b"never executed")
    checksum = tmp_path / "checksums.txt"
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest() if scenario != "checksum-mismatch" else "0" * 64
    checksum.write_text(f"{digest}  {root}.zip\n")
    app = tmp_path / "non-ascii-КароX"
    previous = app / "portable"
    previous.mkdir(parents=True)
    (previous / "previous.txt").write_text("preserve on failure")
    command = powershell_functions(["Remove-PathIfExists", "Expand-KaroXSafeZip", "Install-PortableIfAvailable"]) + f"""
$AppRoot = {ps_literal(app)}
$env:TEMP = {ps_literal(tmp_path)}
$Branch = '{TAG}'
$Repository = 'kar0777/KaroX'
function Enable-KaroXPath {{ Write-Host 'PATH helper invoked (mock: no environment change)' }}
function Get-KaroXDownload {{
    param($Uri, $OutFile, $MaxBytes, [switch]$AllowMissing)
    if ('{scenario}' -eq 'offline') {{ throw 'fixture download failed: offline' }}
    if ('{scenario}' -eq '404' -and $AllowMissing) {{ return $false }}
    if ($Uri.EndsWith('.zip')) {{ Copy-Item -LiteralPath {ps_literal(fixture)} -Destination $OutFile }}
    else {{ Copy-Item -LiteralPath {ps_literal(checksum)} -Destination $OutFile }}
    return $true
}}
$installed = Install-PortableIfAvailable
Write-Host "INSTALLED=$installed"
"""
    result = subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", timeout=20)
    if scenario == "success":
        assert result.returncode == 0, result.stderr
        assert "INSTALLED=True" in result.stdout
        assert "PATH helper invoked" in result.stdout
        assert (previous / "uv.exe").is_file()
        assert not (previous / "previous.txt").exists()
        assert '"%~dp0portable\\karox.cmd" %*' in (app / "KaroX.cmd").read_text()
    else:
        assert (previous / "previous.txt").read_text() == "preserve on failure"
        if scenario == "404":
            assert result.returncode == 0, result.stderr
            assert "INSTALLED=False" in result.stdout
        else:
            assert result.returncode != 0
            assert "checksum" in result.stderr if scenario == "checksum-mismatch" else "fixture" in result.stderr if scenario == "offline" else "runtime is missing" in result.stderr
        assert not (app / "KaroX.cmd").exists()
    assert not list(app.glob(".portable-*"))
    assert not list(tmp_path.glob("karox-portable-*.zip"))
    assert not list(tmp_path.glob("karox-checksums-*.txt"))


def test_posix_launcher_quotes_shell_metacharacters(posix_bootstrap):
    script, env, _ = posix_bootstrap
    root = Path(env["FIXTURE_ROOT"]) / "install $(touch INJECTED) `touch INJECTED2` ' quoted"
    env["KAROX_INSTALL_ROOT"] = str(root)
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode == 0, result.stderr
    launch = subprocess.run([str(root / "karox")], env=env, cwd=script.parent, capture_output=True, timeout=5)
    assert launch.returncode == 99
    assert not (script.parent / "INJECTED").exists()
    assert not (script.parent / "INJECTED2").exists()


def test_posix_refuses_untrusted_redirect_before_request(posix_bootstrap):
    _, env, _ = posix_bootstrap
    curl = Path(env["FIXTURE_ROOT"]) / "bin/curl"
    s = curl.read_text()
    s = s.replace("print(status, end='')", "pathlib.Path(args[args.index('--dump-header') + 1]).write_text('Location: https://evil.example/asset\\r\\n')\nprint('302', end='')")
    curl.write_text(s)
    result = run_bootstrap(posix_bootstrap)
    assert result.returncode != 0
    assert "untrusted HTTPS" in result.stderr
    assert len((Path(env["FIXTURE_ROOT"]) / "requests.jsonl").read_text().splitlines()) == 1


def test_powershell_source_fallback_preserves_existing_checkout():
    source = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    assert "$SourceDir = $PSScriptRoot" in source
    assert "Source installer or guard missing; preserving previous installation." in source
    assert "Remove-PathIfExists $SourceDir" not in source
    assert "Move-Item -LiteralPath $sourceBackup -Destination $SourceDir" in source
    assert "'setlocal DisableDelayedExpansion'" in source
    assert 'call "%~dp0portable' not in source
