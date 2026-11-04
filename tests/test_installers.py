"""The one-line installers, driven against a fake pipx.

Each scenario runs the real ``scripts/install.sh`` (POSIX sh) or
``scripts/install.ps1`` (PowerShell, where available) with ``KAROX_PIPX``
pointing at a recording fake. The fake answers ``list --json`` with the
scenario's venvs and creates a real (pip-less) venv for a suffixed install, so
the data-channel ``.pth`` is written into a genuine site-packages directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = "karox-runtime-test-spec"
FAKE_PIPX = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_PIPX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if args[:2] == ["list", "--json"]:
    print(os.environ.get("FAKE_PIPX_LIST") or '{"venvs": {}}')
elif args[:1] == ["environment"]:
    print(os.environ["FAKE_PIPX_VENVS"])
elif args[:1] == ["install"] and "--suffix" in args:
    target = os.path.join(os.environ["FAKE_PIPX_VENVS"], "karox-runtime6")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", target], check=True)
'''


def _venvs(**versions: str) -> str:
    return json.dumps(
        {
            "venvs": {
                name: {"metadata": {"main_package": {"package_version": version}}}
                for name, version in versions.items()
            }
        }
    )


def _powershell() -> Optional[str]:
    if os.name != "nt":
        return None
    return shutil.which("powershell") or shutil.which("pwsh")


SHELLS = [
    pytest.param(
        "sh",
        marks=pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX sh"),
    ),
    pytest.param(
        "powershell",
        marks=pytest.mark.skipif(_powershell() is None, reason="Windows PowerShell"),
    ),
]


def _install(
    shell: str,
    tmp_path: Path,
    listing: str = "",
    *,
    env: Optional[dict[str, str]] = None,
    karox_version: Optional[str] = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "fake_pipx.py"
    script.write_text(FAKE_PIPX, encoding="utf-8")
    if shell == "sh":
        pipx = bin_dir / "pipx-fake"
        pipx.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        pipx.chmod(0o755)
        if karox_version:
            karox = bin_dir / "karox"
            karox.write_text(f"#!/bin/sh\necho 'karox {karox_version}'\n", encoding="utf-8")
            karox.chmod(0o755)
        path = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"])
        argv = ["sh", str(ROOT / "scripts" / "install.sh")]
    else:
        pipx = bin_dir / "pipx-fake.cmd"
        pipx.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        if karox_version:
            (bin_dir / "karox.cmd").write_text(f"@echo karox {karox_version}\r\n", encoding="utf-8")
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        path = os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.path.join(windows, "System32"), windows,
             os.path.join(windows, "System32", "WindowsPowerShell", "v1.0")]
        )
        argv = [str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "scripts" / "install.ps1")]
    venvs = tmp_path / "venvs"
    venvs.mkdir(exist_ok=True)
    log = tmp_path / "pipx.log"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KAROX_") and key.upper() != "PATH"
    }
    environment.update(
        {
            "PATH": path,
            "KAROX_PIPX": str(pipx),
            "KAROX_SPEC": SPEC,
            "FAKE_PIPX_LOG": str(log),
            "FAKE_PIPX_LIST": listing,
            "FAKE_PIPX_VENVS": str(venvs),
        }
    )
    environment.update(env or {})
    completed = subprocess.run(argv, capture_output=True, text=True, env=environment, timeout=180)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return completed, calls, venvs


def _installs(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:1] == ["install"]]


def _channel_file(venvs: Path) -> Optional[Path]:
    found = list((venvs / "karox-runtime6").rglob("karox6_channel.pth"))
    return found[0] if found else None


@pytest.mark.parametrize("shell", SHELLS)
def test_fresh_machine_installs_karox(shell: str, tmp_path: Path) -> None:
    completed, calls, venvs = _install(shell, tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", SPEC]]
    assert _channel_file(venvs) is None


@pytest.mark.parametrize("shell", SHELLS)
def test_karox5_gets_karox6_next_to_it_with_its_own_channel(shell: str, tmp_path: Path) -> None:
    completed, calls, venvs = _install(shell, tmp_path, _venvs(**{"karox-runtime": "5.0.0rc4"}))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", "--suffix", "6", SPEC]]
    channel = _channel_file(venvs)
    assert channel is not None
    assert channel.read_text(encoding="utf-8").strip() == 'import os; os.environ.setdefault("KAROX_CHANNEL", "dogfood")'
    assert "karox6" in completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_an_existing_karox6_is_left_alone_unless_reinstall(shell: str, tmp_path: Path) -> None:
    listing = _venvs(**{"karox-runtime": "5.0.0rc4", "karox-runtime6": "6.0.0a3"})
    completed, calls, _ = _install(shell, tmp_path, listing)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "already installed as 'karox6'" in completed.stdout
    completed, calls, _ = _install(shell, tmp_path, listing, env={"KAROX_REINSTALL": "1"})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls)[-1] == ["install", "--force", "--suffix", "6", SPEC]


@pytest.mark.parametrize("shell", SHELLS)
def test_refuses_to_replace_karox5_when_side_by_side_is_off(shell: str, tmp_path: Path) -> None:
    completed, calls, _ = _install(
        shell, tmp_path, _venvs(**{"karox-runtime": "5.0.0rc4"}), env={"KAROX_SIDE_BY_SIDE": "0"}
    )
    assert completed.returncode != 0
    assert _installs(calls) == []
    assert "would replace it" in completed.stdout + completed.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_main_karox6_install_is_not_touched(shell: str, tmp_path: Path) -> None:
    completed, calls, _ = _install(shell, tmp_path, _venvs(**{"karox-runtime": "6.0.0a3"}))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "already installed as 'karox'" in completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_karox5_outside_pipx_is_still_detected(shell: str, tmp_path: Path) -> None:
    completed, calls, _ = _install(shell, tmp_path, karox_version="5.0.0rc4")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == [["install", "--suffix", "6", SPEC]]


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_changes_nothing(shell: str, tmp_path: Path) -> None:
    completed, calls, venvs = _install(
        shell, tmp_path, _venvs(**{"karox-runtime": "5.0.0rc4"}), env={"KAROX_DRY_RUN": "1"}
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _installs(calls) == []
    assert "would run:" in completed.stdout and "--suffix 6" in completed.stdout
    assert not (venvs / "karox-runtime6").exists()
