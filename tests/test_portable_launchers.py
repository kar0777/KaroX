"""Security and portability contracts for KaroX's no-system-Python launchers."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from _support import ROOT


def _working_bash() -> str | None:
    """A bash that can actually execute a script file, or None.

    On Windows ``shutil.which("bash")`` often finds the WSL launcher
    ``C:\\WINDOWS\\system32\\bash.EXE``, which cannot run a script placed on
    the Windows filesystem (and hangs or fails when no distribution is
    ready). The probe below is the contract this test needs: run a script
    from the temp tree and observe its exit code. Anything else is skipped,
    not failed: the hosted ubuntu and macOS runners exercise the real path.
    """
    bash = shutil.which("bash")
    if bash is None:
        return None
    with tempfile.TemporaryDirectory() as raw:
        probe = Path(raw) / "probe.sh"
        probe.write_text("#!/usr/bin/env sh\nexit 7\n", encoding="utf-8")
        probe.chmod(0o755)
        try:
            result = subprocess.run(
                [bash, str(probe)], capture_output=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    return bash if result.returncode == 7 else None


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_posix_portable_launcher_uses_bundled_uv_and_managed_python() -> None:
    text = _read("portable_launch.sh")
    assert 'UV="$BUNDLE_DIR/uv"' in text
    assert '"$UV" --no-config venv --managed-python --python 3.12 "$VENV_DIR"' in text
    assert 'exec "$PYTHON" -m karox.cli "$@"' in text
    assert "command -v python" not in text
    assert "python3" not in text


def test_posix_portable_launcher_never_clobbers_the_caller_arguments() -> None:
    """`bundle/karox --version` must print the karox version, not the wheel.

    The launcher locates its wheel without `set --`: replacing the positional
    parameters there forwarded the wheel path to the karox CLI instead of the
    user's arguments, which is exactly how the macOS portable smoke caught it.
    """
    text = _read("portable_launch.sh")
    assert "set -- " not in text


def test_posix_portable_launcher_forwards_arguments_verbatim() -> None:
    """End to end: the user's arguments reach the karox CLI unchanged."""
    bash = _working_bash()
    if bash is None:
        pytest.skip("no working bash on this host")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bundle = root / "bundle"
        venv = root / "runtime" / ".venv" / "bin"
        bundle.mkdir(parents=True)
        venv.mkdir(parents=True)
        uv = bundle / "uv"
        uv.write_text(
            "#!/usr/bin/env sh\n"
            # The launcher calls `uv venv` (already present) and `uv pip
            # install` once; both succeed without touching the filesystem.
            "exit 0\n",
            encoding="utf-8",
        )
        python = venv / "python"
        python.write_text(
            "#!/usr/bin/env sh\n"
            'printf \'%s\\n\' "$@"\n',
            encoding="utf-8",
        )
        (bundle / "karox_runtime-5.0.0rc2-py3-none-any.whl").write_bytes(b"w")
        for path in (uv, python):
            path.chmod(0o755)
        launcher = bundle / "karox"
        launcher.write_text(
            (ROOT / "scripts" / "portable_launch.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        result = subprocess.run(
            [str(bash), str(launcher), "--version", "quickstart", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(root / "home"),
                "KAROX_RUNTIME_DIR": str(root / "runtime"),
                "KAROX_CONFIG_DIR": str(root / "config"),
            },
        )
        assert result.returncode == 0, result.stderr
        # The real launcher execs `-m karox.cli`; the stub echoes its argv.
        forwarded = [line for line in result.stdout.splitlines() if line]
        assert "-m" in forwarded and "karox.cli" in forwarded
        assert "--version" in forwarded
        assert "quickstart" in forwarded and "--json" in forwarded
        assert not any(name.endswith(".whl") for name in forwarded)


def test_windows_portable_launcher_uses_bundled_uv_and_managed_python() -> None:
    text = _read("portable_launch.ps1")
    assert 'Join-Path $BundleDir "uv.exe"' in text
    assert "--no-config venv --managed-python --python 3.12" in text
    assert "& $Python -m karox.cli @args" in text
    assert "Get-Command python" not in text
    assert "winget" not in text.lower()


def test_portable_launchers_do_not_download_or_execute_remote_bootstrap_code() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    for text in (shell, powershell):
        lowered = text.lower()
        assert "curl " not in lowered
        assert "wget " not in lowered
        assert "invoke-webrequest" not in lowered
        assert "invoke-restmethod" not in lowered
        assert "iex" not in lowered


def test_portable_launchers_ignore_ambient_package_indexes_and_python_mirrors() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    for name in (
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "UV_PYTHON_INSTALL_MIRROR",
        "UV_PYTHON_CPYTHON_MIRROR",
    ):
        assert name in shell
        assert name in powershell
    assert "--no-config pip install" in shell
    assert "--no-config pip install" in powershell
    assert "--no-build" in shell
    assert "--no-build" in powershell


def test_portable_launchers_keep_runtime_and_config_outside_bundle() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    assert "KAROX_RUNTIME_DIR" in shell and "KAROX_CONFIG_DIR" in shell
    assert "KAROX_RUNTIME_DIR" in powershell and "KAROX_CONFIG_DIR" in powershell
    assert "portable-wheel.txt" in shell
    assert "portable-wheel.txt" in powershell
